import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from refinement import RefinementBlock  # 从别的文件里面调用的，用于最终输出时的优化
from laynorm import LayerNorm2d
import gc


class FusedPooling(nn.Module):
    # fusedpooling融合池化，在distill block的distiller当中，表示两个并行的池化操作，即avg_pool 和 max_pool
    def __init__(self, nc):
        super(FusedPooling, self).__init__()

        self.avg_pool = nn.AvgPool2d(kernel_size=2, stride=2)
        self.max_pool = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv = nn.Conv2d(in_channels=2 * nc, out_channels=nc, kernel_size=1, padding=0, bias=False)
        # 在torch.nn下包含了我们模型的概念、一些常用层、损失函数等的定义。conv2d是用来实现2d卷积操作的类。

    def forward(self, x):
        # fusedpooling的前向传播函数forward
        x_ap = self.avg_pool(x)
        x_mp = self.max_pool(x)
        return self.conv(torch.cat((x_ap, x_mp), dim=1))


class Block(nn.Module):
    # Block对应模块3*3 conv+LeakyReLU+3*3 conv+LeakyReLU
    def __init__(self, in_ch, out_ch, k_sz=3):
        super(Block, self).__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch // 2, k_sz, padding=k_sz // 2, padding_mode="reflect", bias=True)
        self.op1 = nn.LeakyReLU(0.2)
        self.conv2 = nn.Conv2d(out_ch // 2, out_ch, k_sz, padding=k_sz // 2, padding_mode="reflect", bias=True)
        self.op2 = nn.LeakyReLU(0.2)

    def forward(self, x):
        # x通常是一个四维张量，其形状为(batch_size, num_channels, height, width)
        # 该模块的前向函数
        # print(f"通道数：{x.shape[1]}")
        x = self.op1(self.conv1(x))
        x = self.op2(self.conv2(x))
        return x


class DynamicConvolution(nn.Module):
    # 动态卷积，存在于distill block中，可在初始化函数中看到对应方法是nn.conv2d

    def __init__(self, in_ch, out_ch, k, stride, num_cnvs, device='cuda'):
        super(DynamicConvolution, self).__init__()

        self.convs = nn.ModuleList([nn.Conv2d(in_ch, out_ch, k, stride, padding='same', padding_mode='reflect')
                                    for _ in range(num_cnvs)])
        self.weights = nn.Parameter(1 / num_cnvs * torch.ones((num_cnvs, 1), device=device, dtype=torch.float),
                                    requires_grad=True)

    def forward(self, x):
        # dynconv的前向传播函数forward
        feats = 0
        for i, conv in enumerate(self.convs):
            feats += self.weights[i] * conv(x)

        return feats


class DynamicConvolutionT(nn.Module):
    # 转置卷积，存在于decoder block中,可在初始化函数中看到对应方法是nn.ConvTranspose2d
    def __init__(self, in_ch, out_ch, k, stride, num_cnvs, device='cuda'):
        super(DynamicConvolutionT, self).__init__()

        self.convs = nn.ModuleList([nn.ConvTranspose2d(in_ch, out_ch, k, stride) for _ in range(num_cnvs)])
        self.weights = nn.Parameter(1 / num_cnvs * torch.ones((num_cnvs, 1), device=device, dtype=torch.float),
                                    requires_grad=True)

    def forward(self, x):
        # dynconvT的前向传播函数forward
        feats = 0
        for i, conv in enumerate(self.convs):
            feats += self.weights[i] * conv(x)

        return feats


class EncoderBlock(nn.Module):
    # 下采样，可在初始化函数中看到对应方法是Block, nn.conv2d, nn.InstanceNorm2d, nn.LeakyReLu
    # 分别对应（与paper中fig.3）：block--3*3 conv+LeakyReLU+3*3 conv+LeakyReLU; nn.conv2d--strided conv步长=2>1;
    # nn.InstanceNorm2d--Inst. Norm归一化加速模型收敛; nn.LeakyReLu--与ReLU相似的激活函数
    def __init__(self, in_ch, out_ch, device='cuda'):
        super(EncoderBlock, self).__init__()
        self.in_ch = in_ch
        self.out_ch = out_ch

        self.in_block = Block(in_ch, out_ch)
        self.compress_op = nn.Conv2d(in_channels=out_ch, out_channels=out_ch, kernel_size=2, stride=2, bias=False)
        self.norm = nn.InstanceNorm2d(out_ch)
        self.op = nn.LeakyReLU(0.2)

    def forward(self, x):
        # 下采样阶段前向传播函数
        # print(f"通道数：{x.shape[1]}")
        block_feats = self.in_block(x)
        out_feats = self.op(self.norm(self.compress_op(block_feats)))
        return out_feats


class SCAM(nn.Module):
    # 立体交叉注意力模块Stereo Cross Attention Module在decoder中，融合DistillBlock信息+skip connection信息，用于增强低分辨率图像的纹理
    def __init__(self, c):
        super().__init__()
        self.scale = c ** -0.5

        self.norm_l = LayerNorm2d(c)
        self.norm_r = LayerNorm2d(c)
        self.l_proj1 = nn.Conv2d(c, c, kernel_size=1, stride=1, padding=0)
        self.r_proj1 = nn.Conv2d(c, c, kernel_size=1, stride=1, padding=0)

        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)
        self.gamma = nn.Parameter(torch.zeros((1, c, 1, 1)), requires_grad=True)

        self.l_proj2 = nn.Conv2d(c, c, kernel_size=1, stride=1, padding=0)
        self.r_proj2 = nn.Conv2d(c, c, kernel_size=1, stride=1, padding=0)

    def forward(self, x_l, x_r):
        Q_l = self.l_proj1(self.norm_l(x_l)).permute(0, 2, 3, 1)  # B, H, W, c 左=中间DistillBlock曝光调整出来的结果=查询，假设为想要查询的关键词
        # x_l分辨率&通道数532 256
        Q_r_T = self.r_proj1(self.norm_r(x_r)).permute(0, 2, 1, 3)  # B, H, c, W 右=跳跃连接EncoderBlock出来的特征图=键，用于帮助定位到含关键词的所有部分（包含想要和不想要的所有部分）
        # x_r分辨率&通道数32 256

        V_l = self.l_proj2(x_l).permute(0, 2, 3, 1)  # B, H, W, c 值，与查询/关键词相关的实际内容
        V_r = self.r_proj2(x_r).permute(0, 2, 3, 1)  # B, H, W, c 值

        # (B, H, W, c) x (B, H, c, W) -> (B, H, W, W)
        attention = torch.matmul(Q_l, Q_r_T) * self.scale # (Q_l, Q_r_T)左查询右键，说明了Q_r_T相较于Q_l每个位置的关联程度
        # Q_l形状B,532,532,256   Q_r_T形状B,32,256Q_l,32 结果attention形状B,532,532,32
        # 注意力机制常通过比较查询和键的关联度以确定值的重要性
        # 这里使用点积注意力机制，得到一个注意力分数矩阵

        F_r2l = torch.matmul(torch.softmax(attention, dim=-1), V_r)  # B, H, W, c  结果形状(b, 532, 532, 256)
        # 根据得到的注意力分数，对值/右侧特征图进行加权求和，得到从右侧到左侧的特征更新
        F_l2r = torch.matmul(torch.softmax(attention.permute(0, 1, 3, 2), dim=-1), V_l)  # B, H, W, c   结果形状(b, 532, 32, 256)
        # 同上理

        # scale
        F_r2l = F_r2l.permute(0, 3, 1, 2) * self.beta # B,H,W,c 特征缩放    (b, 256, 532, 532)
        F_l2r = F_l2r.permute(0, 3, 1, 2) * self.gamma # B,H,W,c          (b, 256, 532, 32)
        return torch.cat((x_l + F_r2l,  x_r + F_l2r), dim=1)
        # 将调整后的特征更新与原始输入进行逐元素相加，得到更新后的左右特征图，并将它们连接在一起，作为模块的输出。


class DecoderBlock(nn.Module):
    # 上采样，可在初始化函数中看到对应方法是DynamicConvolutionT, nn.InstanceNorm2d, Block, nn.ReLu, SCAM
    def __init__(self, in_ch, out_ch, device='cuda'):
        super(DecoderBlock, self).__init__()

        self.in_ch = in_ch
        self.out_ch = out_ch

        self.dconv = DynamicConvolutionT(in_ch, out_ch, 2, stride=2, num_cnvs=4, device=device)
        self.norm = nn.InstanceNorm2d(out_ch)
        self.block = Block(2 * out_ch, out_ch)
        self.op = nn.ReLU()
        self.scam = SCAM(out_ch)

    def forward(self, x, skip_conn):
        # 上采样传播分为skip connection和DynConvT两部分
        y = self.scam(self.dconv(x), skip_conn)
        y = self.op(self.norm(self.block(y)))
        return y


class ChannelAttention(nn.Module):
    # ChannelAttention模块，在distill block中，从该模块输出weighted feature map
    def __init__(self, num_channel):
        super(ChannelAttention, self).__init__()

        self.model = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels=num_channel, out_channels=num_channel // 2, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(in_channels=num_channel // 2, out_channels=num_channel, kernel_size=1, padding=0, stride=1,
                      groups=1, bias=True),
            nn.Sigmoid()
        )

    def forward(self, x):
        # ChannelAttention向前传播
        out = self.model(x)
        return out


class Distiller(nn.Module):
    # distiller模块
    def __init__(self, num_chann, factor):
        super(Distiller, self).__init__()

        op_list = []

        num_ops = int(np.log2(factor))

        in_chann = 3
        for _ in range(num_ops): # num_ops控制distiller中有几个CONV+RELU+FUSEDPOOLING循环
            out_chann = in_chann * 2
            op_list += [
                nn.Conv2d(in_channels=in_chann, out_channels=out_chann, kernel_size=3, padding='same'),
                nn.ReLU(),
                FusedPooling(out_chann)
            ]
            in_chann = out_chann

        self.encoder = nn.Sequential(*op_list)
        self.out_conv = nn.Conv2d(in_channels=in_chann, out_channels=num_chann, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        return torch.sigmoid(self.out_conv(self.encoder(x)))


class InnerMapBlock(nn.Module):
    # 等同于distill block，把所有整合起来
    def __init__(self, num_chann, num_ops, factor, device="cuda"):
        super(InnerMapBlock, self).__init__()
        self.num_chann = num_chann
        self.num_ops = num_ops
        self.factor = factor

        self.cam = ChannelAttention(num_channel=num_chann)
        self.xconv = DynamicConvolution(num_chann, num_chann, k=3, stride=1, num_cnvs=num_ops, device=device) # xe5输入分辨率&通道数不变
        self.ln = LayerNorm2d(num_chann)
        self.distiller = Distiller(num_chann, factor)

    def forward(self, x, key_info):
        x_att = self.cam(x)
        x_conv = self.ln(self.xconv(x))
        x_key = self.distiller(key_info)
        return x_att * x_conv + x_key


class DistillNet(nn.Module):
    # 把所有网络整合到一起，优化部分refinement单独文件
    def __init__(self, num_iblocks, num_ops, device="cuda"):
        super(DistillNet, self).__init__()
        self.num_iblocks = num_iblocks
        self.alphas = nn.Parameter(1 / num_iblocks * torch.ones((num_iblocks, 1), device=device, dtype=torch.float),
                                    requires_grad=True)
        self.omegas = nn.Parameter(1 / num_iblocks * torch.ones((num_iblocks, 1), device=device, dtype=torch.float),
                                    requires_grad=True)
        self.a1 = nn.Parameter(torch.tensor(0.5, device=device, dtype=torch.float32), requires_grad=True)
        self.a2 = nn.Parameter(torch.tensor(0.5, device=device, dtype=torch.float32), requires_grad=True)

        self.e1 = EncoderBlock(3, 32, device=device)
        self.e2 = EncoderBlock(32, 64, device=device)
        self.e3 = EncoderBlock(64, 128, device=device)
        self.e4 = EncoderBlock(128, 256, device=device)
        self.e5 = EncoderBlock(256, 512, device=device)

        inner_blocks = []
        for _ in range(num_iblocks): # num_iblocks控制有几个论文图中distill block
            inner_blocks.append(InnerMapBlock(512, num_ops, factor=32, device=device))
        self.inner_stage = nn.ModuleList(inner_blocks)

        self.d5 = DecoderBlock(512, 256, device=device)
        self.d4 = DecoderBlock(256, 128, device=device)
        self.d3 = DecoderBlock(128, 64, device=device)
        self.d2 = DecoderBlock(64, 32, device=device)
        self.d1 = DecoderBlock(32, 3, device=device)

        self.out_conv_fg = RefinementBlock(3, 3, k_sz=5)
        self.out_conv_bg = RefinementBlock(3, 3, k_sz=5)

    def forward(self, x, mask):
        xe1 = self.e1(x)
        # gc.collect()
        # torch.cuda.empty_cache()
        xe2 = self.e2(xe1)
        xe3 = self.e3(xe2)
        xe4 = self.e4(xe3)
        xe5 = self.e5(xe4)

        xi = xe5
        for i, block in enumerate(self.inner_stage):
            alpha = torch.sigmoid(self.alphas[i]) * (1 - mask)
            omega = (1 + torch.tanh(self.omegas[i])) * mask
            inp_info = torch.clamp(x * (alpha + omega), 0, 1) # x就是train.py里面的512tile
            xi = block(xi, inp_info) # 对应class InnerMapBlock中def forward(self, x, key_info)的(x, key_info)
            # 当i=0时，对应只有一个block，对应上面inner_blocks只append一个
            # inp_info分辨率512通道数3, xi=xe5是分辨率16通道数512

        xd5 = self.d5(xi, xe4)
        xd4 = self.d4(xd5, xe3)
        xd3 = self.d3(xd4, xe2)
        xd2 = self.d2(xd3, xe1)
        xd1 = self.d1(xd2, x)

        out_1 = x + torch.tanh(self.out_conv_fg(xd1))
        out_2 = torch.clamp(torch.tanh(x + self.out_conv_bg(xd1)), min=0, max=1)
        return torch.sigmoid(self.a1) * out_1 + torch.sigmoid(self.a2) * out_2

