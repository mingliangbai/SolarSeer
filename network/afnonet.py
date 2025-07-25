# Copyright (c) 2022, FourCastNet authors
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The code was authored by the following people:
#
# Jaideep Pathak - NVIDIA Corporation
# Shashank Subramanian - NERSC, Lawrence Berkeley National Laboratory
# Peter Harrington - NERSC, Lawrence Berkeley National Laboratory
# Sanjeev Raja - NERSC, Lawrence Berkeley National Laboratory
# Ashesh Chattopadhyay - Rice University
# Morteza Mardani - NVIDIA Corporation
# Thorsten Kurth - NVIDIA Corporation
# David Hall - NVIDIA Corporation
# Zongyi Li - California Institute of Technology, NVIDIA Corporation
# Kamyar Azizzadenesheli - Purdue University
# Pedram Hassanzadeh - Rice University
# Karthik Kashinath - NVIDIA Corporation
# Animashree Anandkumar - California Institute of Technology, NVIDIA Corporation

from functools import partial
import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.layers import DropPath, trunc_normal_
import torch.fft
from einops import rearrange
from torchvision.transforms import CenterCrop


def add(x_list):
    for _x in x_list[1:]:
        x_list[0] += _x
    return x_list[0]


def calculate_original_values(min_orig, max_orig, num_classes):
    interval_width = (max_orig - min_orig) / num_classes
    original_values = torch.linspace(min_orig + interval_width/2, max_orig - interval_width/2, num_classes)
    return original_values


def process_input(inputs, func, params):
    return func(inputs, **params)


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class AFNO2D(nn.Module):
    def __init__(self, hidden_size, num_blocks=8, sparsity_threshold=0.01,
                 hard_thresholding_fraction=1, hidden_size_factor=1):
        super().__init__()
        assert hidden_size % num_blocks == 0, \
            f"hidden_size {hidden_size} should be divisble by num_blocks {num_blocks}"

        self.hidden_size = hidden_size
        self.sparsity_threshold = sparsity_threshold
        self.num_blocks = num_blocks
        self.block_size = self.hidden_size // self.num_blocks
        self.hard_thresholding_fraction = hard_thresholding_fraction
        self.hidden_size_factor = hidden_size_factor
        self.scale = 0.02

        self.w1 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks, self.block_size,
                                                        self.block_size * self.hidden_size_factor))
        self.b1 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks,
                                                        self.block_size * self.hidden_size_factor))
        self.w2 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks, self.block_size * self.hidden_size_factor,
                                                        self.block_size))
        self.b2 = nn.Parameter(self.scale * torch.randn(2, self.num_blocks, self.block_size))

    def forward(self, x):
        bias = x

        dtype = x.dtype
        x = x.float()
        B, H, W, C = x.shape

        x = torch.fft.rfft2(x, dim=(1, 2), norm="ortho")
        x = x.reshape(B, H, W // 2 + 1, self.num_blocks, self.block_size)

        o1_real = torch.zeros([B, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor],
                              device=x.device)
        o1_imag = torch.zeros([B, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor],
                              device=x.device)
        o2_real = torch.zeros(x.shape, device=x.device)
        o2_imag = torch.zeros(x.shape, device=x.device)

        total_modes = H // 2 + 1
        kept_modes = int(total_modes * self.hard_thresholding_fraction)

        o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].real,
                         self.w1[0]) -
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].imag,
                         self.w1[1]) +
            self.b1[0]
        )

        o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].imag,
                         self.w1[0]) +
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].real,
                         self.w1[1]) +
            self.b1[1]
        )

        o2_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = (
            torch.einsum('...bi,bio->...bo', o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes],
                         self.w2[0]) -
            torch.einsum('...bi,bio->...bo', o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes],
                         self.w2[1]) +
            self.b2[0]
        )

        o2_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = (
            torch.einsum('...bi,bio->...bo', o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes],
                         self.w2[0]) +
            torch.einsum('...bi,bio->...bo', o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes],
                         self.w2[1]) +
            self.b2[1]
        )

        x = torch.stack([o2_real, o2_imag], dim=-1)
        x = F.softshrink(x, lambd=self.sparsity_threshold)
        x = torch.view_as_complex(x)
        x = x.reshape(B, H, W // 2 + 1, C)
        x = torch.fft.irfft2(x, s=(H, W), dim=(1, 2), norm="ortho")
        x = x.type(dtype)

        return x + bias


class Block(nn.Module):
    def __init__(
            self,
            dim,
            mlp_ratio=4.,
            drop=0.,
            drop_path=0.,
            act_layer=nn.GELU,
            norm_layer=nn.LayerNorm,
            double_skip=True,
            num_blocks=8,
            sparsity_threshold=0.01,
            hard_thresholding_fraction=1.0
    ):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.filter = AFNO2D(dim, num_blocks, sparsity_threshold, hard_thresholding_fraction)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        # self.drop_path = nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.double_skip = double_skip

    def forward(self, x):
        residual = x
        x = self.norm1(x)
        x = self.filter(x)

        if self.double_skip:
            x = x + residual
            residual = x

        x = self.norm2(x)
        x = self.mlp(x)
        x = self.drop_path(x)
        x = x + residual
        return x

class PatchEmbed(nn.Module):
    def __init__(self, img_size=(224, 224), patch_size=(16, 16), in_chans=3, embed_dim=768):
        super().__init__()
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, C, H, W = x.shape
        assert H == self.img_size[0] and W == self.img_size[1], f"Input image size + \
            ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x
  
    
class PeriodicPad2d(nn.Module):
    """
        pad longitudinal (left-right) circular
        and pad latitude (top-bottom) with zeros
    """
    def __init__(self, pad_width):
        super(PeriodicPad2d, self).__init__()
        self.pad_width = pad_width

    def forward(self, x):
        # pad left and right circular
        out = F.pad(x, (self.pad_width, self.pad_width, 0, 0), mode="circular")
        # pad top and bottom zeros
        out = F.pad(out, (0, 0, self.pad_width, self.pad_width), mode="constant", value=0)
        return out


def load_backbone_weight(backbone, weight_path, fix_param=True):
    backbone_weight = torch.load(weight_path)
    backbone.load_state_dict(backbone_weight['module'], strict=True)
    if fix_param:
        for param in backbone.parameters():
            param.requires_grad = False
    return backbone


class AFNONet(nn.Module):
    def __init__(
            self,
            params,
            img_size=(720, 1440),
            patch_size=(16, 16),
            in_chans=2,
            out_chans=2,
            input_time_dim=None,
            output_time_dim=None,
            embed_dim=768,
            depth=12,
            mlp_ratio=4.,
            drop_rate=0.,
            drop_path_rate=0.,
            num_blocks=16,
            sparsity_threshold=0.01,
            hard_thresholding_fraction=1.0,
            autoregressive_steps=1,
            use_dilated_conv_blocks=False,
            output_only_last=False,
            target_variable_index=None,
            **kwargs
    ):
        super().__init__()
        self.params = params
        self.img_size = img_size
        self.patch_size = (params.get('patch_size', patch_size[0]), params.get('patch_size', patch_size[1]))
        self.in_chans = params.get('N_in_channels', in_chans)
        self.out_chans = params.get('N_out_channels', out_chans)
        self.input_time_dim = input_time_dim
        self.output_time_dim = output_time_dim if output_time_dim is not None else input_time_dim
        self.has_time_dim = input_time_dim is not None
        self.num_features = self.embed_dim = embed_dim
        self.num_blocks = num_blocks
        self.use_dilated_conv_blocks = use_dilated_conv_blocks
        self.autoregressive_steps = autoregressive_steps
        self.output_only_last = output_only_last
        self.target_variable_index = target_variable_index
        norm_layer = partial(nn.LayerNorm, eps=1e-6)

        if self.has_time_dim:
            assert embed_dim % self.input_time_dim == 0, 'embed_dim must be divisible by input_time_dim'
            assert embed_dim % self.output_time_dim == 0, 'embed_dim must be divisible by output_time_dim'
            input_patch_embed_dim = embed_dim // self.input_time_dim
            self.output_patch_embed_dim = embed_dim // self.output_time_dim
        else:
            input_patch_embed_dim = embed_dim
            self.output_patch_embed_dim = embed_dim
        self.patch_embed = PatchEmbed(img_size=img_size, patch_size=self.patch_size,
                                      in_chans=self.in_chans, embed_dim=input_patch_embed_dim)
        num_patches = self.patch_embed.num_patches

        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches, input_patch_embed_dim))
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        self.h = img_size[0] // self.patch_size[0]
        self.w = img_size[1] // self.patch_size[1]

        self.blocks = nn.ModuleList([
            Block(dim=embed_dim, mlp_ratio=mlp_ratio, drop=drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                  num_blocks=self.num_blocks, sparsity_threshold=sparsity_threshold,
                  hard_thresholding_fraction=hard_thresholding_fraction)
            for i in range(depth)])

        self.norm = norm_layer(embed_dim)  
        self.head = nn.Linear(self.output_patch_embed_dim,
                              self.out_chans*self.patch_size[0]*self.patch_size[1], bias=False)

        if self.use_dilated_conv_blocks:
            self.crop_layer = CenterCrop(params['target_size'])

        trunc_normal_(self.pos_embed, std=.02)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def forward_features(self, x):
        B = x.shape[0]
        if self.has_time_dim:
            x = rearrange(x, 'b c t h w -> (b t) c h w')
        x = self.patch_embed(x)
        x = x + self.pos_embed
        x = self.pos_drop(x)

        if self.has_time_dim:
            x = rearrange(x, '(b t) (h w) c -> b h w (c t)', h=self.h, w=self.w, t=self.input_time_dim)
        else:
            x = x.reshape(B, self.h, self.w, self.embed_dim)
        for blk in self.blocks:
            x = blk(x)

        if self.has_time_dim:
            x = rearrange(x, 'b h w (c t) -> b t h w c', t=self.output_time_dim)
        return x

    def forward_head(self, x):
        x = self.head(x)
        x = rearrange(
            x,
            "b t h w (p1 p2 c_out) -> b c_out t (h p1) (w p2)" if self.has_time_dim else
            "b h w (p1 p2 c_out) -> b c_out (h p1) (w p2)",
            p1=self.patch_size[0],
            p2=self.patch_size[1],
            h=self.img_size[0] // self.patch_size[0],
            w=self.img_size[1] // self.patch_size[1],
        )
        return x

    def forward_step(self, x):
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x

    def get_next_input(self, inputs, outputs):
        if not self.has_time_dim:
            return outputs[-1]
        elif self.input_time_dim <= self.output_time_dim:
            return outputs[-1][:, :, -self.input_time_dim:]
        else:
            return torch.cat([inputs[:, :, self.output_time_dim:], outputs[-1]], dim=2)

    def forward(self, inputs, decoder_inputs=None):
        output_list = []
        for step in range(self.autoregressive_steps):
            if step > 0:
                inputs = self.get_next_input(inputs, output_list)
            x = self.forward_step(inputs)
            output_list.append(x)
        # For model without time dimension, ignore output_only_last and return the output of the last step
        if self.has_time_dim and not self.output_only_last:
            x = torch.cat(output_list, dim=2)
        else:
            x = output_list[-1]
        if self.use_dilated_conv_blocks:
            x = self.crop_layer(x)
        if self.target_variable_index is not None:
            x = x[:, self.target_variable_index]
        return x


class AFNONetOneStep(AFNONet):
    def __init__(
            self,
            **kwargs
    ):
        super(AFNONetOneStep, self).__init__(**kwargs)

    def forward(self, x):
        x = self.forward_step(x)
        return x


class EncoderAFNONet(AFNONet):
    def __init__(
            self,
            **kwargs
    ):
        super(EncoderAFNONet, self).__init__(**kwargs)

    def forward(self, x):
        x = self.forward_features(x)
        return x


def backbone_load(ckpt_path, kwargs):
    backbone_weight = torch.load(ckpt_path)
    input_chans = backbone_weight['module']['patch_embed.proj.weight'].shape[1]
    num = kwargs['params']['patch_size'] * kwargs['params']['patch_size']
    output_chans = backbone_weight['module']['head.weight'].shape[0] // num
    kwargs['params']['N_in_channels'] = input_chans
    kwargs['params']['N_out_channels'] = output_chans
    backbone = AFNONetOneStep(**kwargs)
    state = backbone_weight['module'].copy()
    for weight_name in state:
        if weight_name.startswith('backbone'):
            del backbone_weight['module'][weight_name]
    backbone.load_state_dict(backbone_weight['module'], strict=True)

    for param in backbone.parameters():
        param.requires_grad = False
    return backbone


class MultiEncoderAFNONet(nn.Module):
    def __init__(
            self,
            multi_params,
            **kwargs
    ):
        super().__init__()
        # multi encoder
        len_encoder = len(multi_params)
        multi_encoder_params_list = multi_params
        self.encoders = []
        self.output_patch_embed_list = []
        for i in range(len_encoder):
            hard_threshold = multi_encoder_params_list[i]['hard_thresholding_fraction']
            module = EncoderAFNONet(params=multi_encoder_params_list[i],
                                    img_size=multi_encoder_params_list[i]['img_size'],
                                    patch_size=(multi_encoder_params_list[i]['patch_size'],
                                                multi_encoder_params_list[i]['patch_size']),
                                    in_chans=multi_encoder_params_list[i]['N_in_channels'],
                                    out_chans=multi_encoder_params_list[i]['N_out_channels'],
                                    input_time_dim=multi_encoder_params_list[i]['input_time_dim'],
                                    output_time_dim=multi_encoder_params_list[i]['output_time_dim'],
                                    embed_dim=multi_encoder_params_list[i]['embed_dim'],
                                    depth=multi_encoder_params_list[i]['depth'],
                                    mlp_ratio=multi_encoder_params_list[i]['mlp_ratio'],
                                    drop_rate=multi_encoder_params_list[i]['drop_rate'],
                                    drop_path_rate=multi_encoder_params_list[i]['drop_path_rate'],
                                    num_blocks=multi_encoder_params_list[i]['num_blocks'],
                                    sparsity_threshold=multi_encoder_params_list[i]['sparsity_threshold'],
                                    hard_thresholding_fraction=hard_threshold,
                                    autoregressive_steps=multi_encoder_params_list[i]['autoregressive_steps'],
                                    use_dilated_conv_blocks=multi_encoder_params_list[i]['use_dilated_conv_blocks'],
                                    output_only_last=multi_encoder_params_list[i]['output_only_last'],
                                    target_variable_index=multi_encoder_params_list[i]['target_variable_index'])
            self.encoders.append(module)
            self.output_patch_embed_list.append(self.encoders[-1].output_patch_embed_dim)

        self.crop_layer = self.encoders[0].crop_layer if kwargs['use_dilated_conv_blocks'] else None
        self.out_chans = self.encoders[0].out_chans
        self.encoders = nn.ModuleList(self.encoders)
        self.autoregressive_steps = kwargs['autoregressive_steps']
        self.patch_size = (multi_encoder_params_list[0]['patch_size'], multi_encoder_params_list[0]['patch_size'])
        self.embed_dim = multi_encoder_params_list[0]['embed_dim']
        self.input_time_dim = multi_encoder_params_list[0]['input_time_dim']
        output_dim = multi_encoder_params_list[0]['output_time_dim']
        self.output_time_dim = self.input_time_dim if output_dim is None else output_dim
        self.has_time_dim = multi_encoder_params_list[0]['input_time_dim'] is not None
        self.output_only_last = multi_encoder_params_list[0]['output_only_last']
        self.target_variable_index = kwargs['target_variable_index']
        self.img_size = multi_encoder_params_list[0]['img_size']

        # add decoder
        self.action = kwargs['action']
        if self.action == 'concat':
            self.head = nn.Linear(sum(self.output_patch_embed_list),
                                  self.out_chans*self.patch_size[0]*self.patch_size[1], bias=False)
        else:
            self.head = nn.Linear(self.output_patch_embed_list[0],
                                  self.out_chans*self.patch_size[0]*self.patch_size[1], bias=False)

        self.apply(self._init_weights)
        self.act_final = kwargs.get('act_final', None)
        if self.act_final is not None:
            if self.act_final == 'Tanh':
                self.act = torch.nn.Tanh()
            elif self.act_final == 'ReLU':
                self.act = torch.nn.ReLU(inplace=True)
            elif self.act_final == 'LeakyReLU':
                self.act = torch.nn.LeakyReLU(0.2, True)
            elif self.act_final == 'ReLU6':
                self.act = torch.nn.ReLU6(inplace=True)
            elif self.act_final == 'Sigmoid':
                self.act = torch.nn.Sigmoid()
            else:
                raise ValueError(f'No such activation funtion.{self.act_final}')

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_step(self, input_list):
        input_encoder_list = []
        for i, module in enumerate(self.encoders):
            x = module(input_list[i])
            input_encoder_list.append(x)
        if self.action == 'add':
            x = process_input(input_encoder_list, add, {})
        elif self.action == 'concat':
            x = process_input(input_encoder_list,  torch.concat, {'dim': -1})
        x = self.head(x)

        x = rearrange(
            x,
            "b t h w (p1 p2 c_out) -> b c_out t (h p1) (w p2)" if self.has_time_dim else
            "b h w (p1 p2 c_out) -> b c_out (h p1) (w p2)",
            p1=self.patch_size[0],
            p2=self.patch_size[1],
            h=self.img_size[0] // self.patch_size[0],
            w=self.img_size[1] // self.patch_size[1],
        )
        return x

    def get_next_input(self, inputs, outputs):
        if not self.has_time_dim:
            return outputs[-1]
        elif self.input_time_dim <= self.output_time_dim:
            return outputs[-1][:, :, -self.input_time_dim:]
        else:
            return torch.cat([inputs[:, :, self.output_time_dim:], outputs[-1]], dim=2)

    def forward(self, inputs, decoder_inputs = None):
        output_list = []
        for step in range(self.autoregressive_steps):
            if step > 0:
                inputs = self.get_next_input(inputs, output_list)
            x = self.forward_step([inputs])
            output_list.append(x)

        # For model without time dimension, ignore output_only_last and return the output of the last step
        if self.has_time_dim and not self.output_only_last:
            x = torch.cat(output_list, dim=2)
        else:
            x = output_list[-1]
        if self.crop_layer is not None:
            x = self.crop_layer(x)
        if self.target_variable_index is not None:
            x = x[:, self.target_variable_index]

        # act the output
        if self.act_final is not None:
            x = self.act(x) / 6 if self.act_final == 'ReLU6' else self.act(x)

        return x
