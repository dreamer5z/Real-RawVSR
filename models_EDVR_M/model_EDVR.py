import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
import functools

from models.align_net import PCD_Align
from models.backbone import TSA_Fusion

def initialize_weights(net_l, scale=1):
    if not isinstance(net_l, list):
        net_l = [net_l]
    for net in net_l:
        for m in net.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                m.weight.data *= scale
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                m.weight.data *= scale
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                init.constant_(m.weight, 1)
                init.constant_(m.bias.data, 0.0)

def make_layer(block, n_layers):
    layers = []
    for _ in range(n_layers):
        layers.append(block())
    return nn.Sequential(*layers)

class ResidualBlock_noBN(nn.Module):

    def __init__(self, nf=64):
        super(ResidualBlock_noBN, self).__init__()
        self.conv1 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.conv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)

        # initialization
        initialize_weights([self.conv1, self.conv2], 0.1)

    def forward(self, x):
        identity = x
        out = F.relu(self.conv1(x), inplace=True)
        out = self.conv2(out)
        return identity + out

class EDVR(nn.Module):
    def __init__(self, nf=64, nframes=5, scale=4, groups=8, front_RBs=5, back_RBs=10):
        super(EDVR, self).__init__()

        # activation function
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

        # extract PCD features
        ResidualBlock_noBN_begin = functools.partial(ResidualBlock_noBN, nf=nf)
        ResidualBlock_noBN_end = functools.partial(ResidualBlock_noBN, nf=nf)
        self.conv_first = nn.Conv2d(3, nf, 3, 1, 1, bias=True)

        self.center = nframes // 2
        self.feature_extraction = make_layer(ResidualBlock_noBN_begin, front_RBs)
        self.fea_L2_conv1 = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.fea_L2_conv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.fea_L3_conv1 = nn.Conv2d(nf, nf, 3, 2, 1, bias=True)
        self.fea_L3_conv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)

        # alignment
        self.pcd_align = PCD_Align(nf=nf, groups=groups)

        # self.non_local_attention = Non_Local_Attention(nf=nf, nframes=nframes)

        # build backbone
        self.tsa_fusion = TSA_Fusion(nf=nf, nframes=nframes, center=self.center)
        # self.tsa_fusion = Weight_Fusion(nf=nf, center_frame=self.center)
        self.recon_trunk = make_layer(ResidualBlock_noBN_end, back_RBs)

        # upsampling
        if scale == 4:
            self.sr_conv1 = nn.Conv2d(nf, nf * 4, 3, 1, 1, bias=True)
            self.sr_conv2 = nn.Conv2d(nf, nf * 4, 3, 1, 1, bias=True)
            self.pixel_shuffle = nn.PixelShuffle(2)
            self.sr_conv3 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
            self.sr_conv4 = nn.Conv2d(nf, 3, 3, 1, 1, bias=True)

        elif scale == 3:
            self.sr_conv1 = nn.Conv2d(nf, nf * 9, 3, 1, 1, bias=True)
            self.pixel_shuffle = nn.PixelShuffle(3)
            self.sr_conv3 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
            self.sr_conv4 = nn.Conv2d(nf, 3, 3, 1, 1, bias=True)

        elif scale == 2:
            self.sr_conv1 = nn.Conv2d(nf, nf * 4, 3, 1, 1, bias=True)
            self.pixel_shuffle = nn.PixelShuffle(2)
            self.sr_conv3 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
            self.sr_conv4 = nn.Conv2d(nf, 3, 3, 1, 1, bias=True)
        else:
            raise Exception('scale {} is not supported!'.format(scale))

        # skip
        # self.skipup1 = nn.Conv2d(4, nf * 4, 3, 1, 1, bias=True)
        # self.skipup2 = nn.Conv2d(nf, 4 * 4, 3, 1, 1, bias=True)

        # parameters
        self.scale = scale

    def forward(self, x):
        # N video frames
        B, N, C, H, W = x.size()
        x_center = x[:, self.center, :, :, :].contiguous()

        # extract features
        # L1
        L1_fea = self.lrelu(self.conv_first(x.view(-1, C, H, W)))
        L1_fea = self.feature_extraction(L1_fea)
        # L2
        L2_fea = self.lrelu(self.fea_L2_conv1(L1_fea))
        L2_fea = self.lrelu(self.fea_L2_conv2(L2_fea))
        # L3
        L3_fea = self.lrelu(self.fea_L3_conv1(L2_fea))
        L3_fea = self.lrelu(self.fea_L3_conv2(L3_fea))

        L1_fea = L1_fea.view(B, N, -1, H, W)
        L2_fea = L2_fea.view(B, N, -1, H // 2, W // 2)
        L3_fea = L3_fea.view(B, N, -1, H // 4, W // 4)

        # alignment
        # ref feature list
        ref_fea_l = [
            L1_fea[:, self.center, :, :, :].clone(), L2_fea[:, self.center, :, :, :].clone(),
            L3_fea[:, self.center, :, :, :].clone()
        ]
        
        aligned_fea = []
        for i in range(N):
            nbr_fea_l = [
                L1_fea[:, i, :, :, :].clone(), L2_fea[:, i, :, :, :].clone(),
                L3_fea[:, i, :, :, :].clone()
            ]

            aligned_fea.append(self.pcd_align(nbr_fea_l, ref_fea_l))
        aligned_fea = torch.stack(aligned_fea, dim=1)  # [B, N, nf, H, W]

        # non_local_feature = self.non_local_attention(aligned_fea)  # [B, N, nf, H, W]

        # build backbone
        fea = self.tsa_fusion(aligned_fea)   # [B, nf, H, W]
        # _,nf,_,_ = fea.size()
        fea = self.recon_trunk(fea)  # [B, nf, H, W]

        # upscale
        if self.scale == 4:
            sr = self.lrelu(self.pixel_shuffle(self.sr_conv1(fea)))
            sr = self.lrelu(self.pixel_shuffle(self.sr_conv2(sr)))
            sr = self.lrelu(self.sr_conv3(sr))
            sr = self.sr_conv4(sr)

            base = F.interpolate(x_center, scale_factor=4, mode='bilinear', align_corners=False)
            out = sr + base

        elif self.scale == 3:
            sr = self.lrelu(self.pixel_shuffle(self.sr_conv1(fea)))
            sr = self.lrelu(self.sr_conv3(sr))
            sr = self.sr_conv4(sr)

            base = F.interpolate(x_center, scale_factor=3, mode='bilinear', align_corners=False)
            out = sr + base

        elif self.scale == 2:
            sr = self.lrelu(self.pixel_shuffle(self.sr_conv1(fea)))
            sr = self.lrelu(self.sr_conv3(sr))
            sr = self.sr_conv4(sr)

            base = F.interpolate(x_center, scale_factor=2, mode='bilinear', align_corners=False)
            out = sr + base

        else:
            raise Exception('scale {} is not supported!'.format(self.scale))

        return out
        # return nbr_fea_l[0], aligned_fea[:, 2], aligned_fea[:, 4], out
