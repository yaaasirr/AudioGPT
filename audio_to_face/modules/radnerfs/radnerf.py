import torch
import torch.nn as nn
import torch.nn.functional as F

from audio_to_face.modules.radnerfs.encoders.encoding import get_encoder
from audio_to_face.modules.radnerfs.renderer import NeRFRenderer
from audio_to_face.modules.radnerfs.cond_encoder import AudioNet, AudioAttNet, MLP
from audio_to_face.modules.radnerfs.utils import trunc_exp


class RADNeRF(NeRFRenderer):
    def __init__(self, hparams):
        super().__init__(hparams)
        self.hparams = hparams
        if hparams['cond_type'] == 'esperanto':
            self.cond_in_dim = 44
        elif hparams['cond_type'] == 'deepspeech':
            self.cond_in_dim = 29
        elif hparams['cond_type'] == 'idexp_lm3d_normalized':
            self.cond_in_dim = 68*3
        else:
            raise NotImplementedError()      
              
        # a prenet that processes the raw condition
        self.cond_out_dim = hparams['cond_out_dim']
        self.cond_win_size = hparams['cond_win_size']
        self.smo_win_size = hparams['smo_win_size']
        self.cond_prenet = AudioNet(self.cond_in_dim, self.cond_out_dim, win_size=self.cond_win_size)

        # a attention net that smoothes the condition feat sequence
        self.with_att = hparams['with_att']
        if self.with_att:
            self.cond_att_net = AudioAttNet(self.cond_out_dim, seq_len=self.smo_win_size)

        # a ambient network that predict the 2D ambient coordinate
        # the ambient grid models the dynamic of canonical face
        # by predict ambient coords given cond_feat, we can be driven the face by either audio or landmark!
        self.grid_type = hparams['grid_type'] # tiledgrid or hashgrid
        self.grid_interpolation_type = hparams['grid_interpolation_type'] # smoothstep or linear
        self.position_embedder, self.position_embedding_dim = get_encoder(self.grid_type, input_dim=3, num_levels=16, level_dim=2, base_resolution=16, log2_hashmap_size=hparams['log2_hashmap_size'], desired_resolution=hparams['desired_resolution'] * self.bound, interpolation=self.grid_interpolation_type)
        self.num_layers_ambient = hparams['num_layers_ambient']
        self.hidden_dim_ambient = hparams['hidden_dim_ambient']
        self.ambient_out_dim = hparams['ambient_out_dim']
        self.ambient_net = MLP(self.position_embedding_dim + self.cond_out_dim, self.ambient_out_dim, self.hidden_dim_ambient, self.num_layers_ambient)
        # the learnable ambient grid
        self.ambient_embedder, self.ambient_embedding_dim = get_encoder(self.grid_type, input_dim=hparams['ambient_out_dim'], num_levels=16, level_dim=2, base_resolution=16, log2_hashmap_size=hparams['log2_hashmap_size'], desired_resolution=hparams['desired_resolution'], interpolation=self.grid_interpolation_type)

        # sigma network
        self.num_layers_sigma = hparams['num_layers_sigma']
        self.hidden_dim_sigma = hparams['hidden_dim_sigma']
        self.geo_feat_dim = hparams['geo_feat_dim']

        self.sigma_net = MLP(self.position_embedding_dim + self.ambient_embedding_dim, 1 + self.geo_feat_dim, self.hidden_dim_sigma, self.num_layers_sigma)

        # color network
        self.num_layers_color = hparams['num_layers_color']    
        self.hidden_dim_color = hparams['hidden_dim_color']
        self.direction_embedder, self.direction_embedding_dim = get_encoder('spherical_harmonics')
        self.color_net = MLP(self.direction_embedding_dim + self.geo_feat_dim + self.individual_embedding_dim, 3, self.hidden_dim_color, self.num_layers_color)

    def cal_cond_feat(self, cond):
        """
        cond: [B, T, Ç]
            if deepspeech, [1/8, T=16, 29]
            if eserpanto, [1/8, T=16, 44]
            if idexp_lm3d_normalized, [1/5, T=1, 204]
        """
        cond_feat = self.cond_prenet(cond)
        if self.with_att:
            cond_feat = self.cond_att_net(cond_feat) # [1, 64] 
        return cond_feat

    def forward(self, position, direction, cond_feat, individual_code):
        """
        position: [N, 3], position, in [-bound, bound]
        direction: [N, 3], direction, nomalized in [-1, 1]
        cond_feat: [1, cond_dim], condition encoding, generated by self.cal_cond_feat
        individual_code: [1, ind_dim], individual code for each timestep
        """
        cond_feat = cond_feat.repeat(position.shape[0], 1) # [1,cond_dim] ==> [N, cond_dim]
        pos_feat = self.position_embedder(position, bound=self.bound) # spatial feat f after E^3_{spatial} 3D grid in the paper

        # ambient
        ambient_inp = torch.cat([pos_feat, cond_feat], dim=1) # audio feat and spatial feat 
        ambient_logit = self.ambient_net(ambient_inp).float() # the MLP after AFE in paper, use float(), prevent performance drop due to amp
        ambient_pos = torch.tanh(ambient_logit) # normalized to [-1, 1], act as the coordinate in the 2D ambient tilegrid
        ambient_feat = self.ambient_embedder(ambient_pos, bound=1) # E^2_{audio} in paper, 2D grid

        # sigma
        h = torch.cat([pos_feat, ambient_feat], dim=-1)
        h = self.sigma_net(h)
        sigma = trunc_exp(h[..., 0])
        geo_feat = h[..., 1:]

        # color
        direction_feat = self.direction_embedder(direction)
        if individual_code is not None:
            color_inp = torch.cat([direction_feat, geo_feat, individual_code.repeat(position.shape[0], 1)], dim=-1)
        else:
            color_inp = torch.cat([direction_feat, geo_feat], dim=-1)
        color_logit = self.color_net(color_inp)
        # sigmoid activation for rgb
        color = torch.sigmoid(color_logit)

        return sigma, color, ambient_pos

    def density(self, position, cond_feat, e=None):
        """
        Calculate Density, this is a sub-process of self.forward 
        """
        cond_feat = cond_feat.repeat(position.shape[0], 1) # [1,cond_dim] ==> [N, cond_dim]
        pos_feat = self.position_embedder(position, bound=self.bound) # spatial feat f after E^3_{spatial} 3D grid in the paper

        # ambient
        ambient_inp = torch.cat([pos_feat, cond_feat], dim=1) # audio feat and spatial feat 
        ambient_logit = self.ambient_net(ambient_inp).float() # the MLP after AFE in paper, use float(), prevent performance drop due to amp
        ambient_pos = torch.tanh(ambient_logit) # normalized to [-1, 1], act as the coordinate in the 2D ambient tilegrid
        ambient_feat = self.ambient_embedder(ambient_pos, bound=1) # E^2_{audio} in paper, 2D grid

        # sigma
        h = torch.cat([pos_feat, ambient_feat], dim=-1)
        h = self.sigma_net(h)
        sigma = trunc_exp(h[..., 0])
        geo_feat = h[..., 1:]

        return {
            'sigma': sigma,
            'geo_feat': geo_feat,
        }

