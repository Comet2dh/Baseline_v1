from pathlib import Path
import argparse
import time
import random
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

import cv2
from visualizer import Visualizer
from dataset import StereoDataset
from dpn import DPN, dpn_init
from stn import STN, stn_init
from utils import *
import os

os.environ["CUDA_VISIBLE_DEVICES"] = "2,3"


def parse_args():
    parser = argparse.ArgumentParser(description='MaterialStereo')
    parser.add_argument('--data-path', type=str, default='./data')
    parser.add_argument('--list-path', type=str, default='./lists')
    parser.add_argument('--ckpt-path', type=str, default='ckpt')
    parser.add_argument('--train-split', type=str,
                        default='20170221_1357,20170222_0715,20170222_1207,20170222_1638,20170223_0920,20170223_1217,20170223_1445,20170224_1022')
    parser.add_argument('--test-split', type=str, default='20170222_0951,20170222_1423,20170223_1639,20170224_0742')
    parser.add_argument('--no-filt', action='store_true')
    parser.add_argument('--no-conf', action='store_true')
    parser.add_argument('--no-mat', action='store_true')
    parser.add_argument('--no-light', action='store_true')
    parser.add_argument('--no-glass', action='store_true')
    parser.add_argument('--no-glossy', action='store_true')
    parser.add_argument('--black-level', type=float, default=2.0)
    parser.add_argument('--clamp-value', type=float, default=5.0)
    parser.add_argument('--clamp-disp', type=float, default=0.04)
    parser.add_argument('--consist', type=float, default=2.0)
    parser.add_argument('--alpha', type=float, default=0.85)
    parser.add_argument('--diffuse-smooth', type=float, default=25)
    parser.add_argument('--light-smooth', type=float, default=3000)
    parser.add_argument('--glass-smooth', type=float, default=1000)
    parser.add_argument('--glossy-smooth', type=float, default=80)
    parser.add_argument('--edge-factor', type=float, default=1.0)
    parser.add_argument('--disp-factor', type=float, default=0.005)
    parser.add_argument('--resume', type=str, default=None)
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--n-epochs', type=int, default=48)
    parser.add_argument('--warmup-epochs', type=int, default=4)
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--decay', type=float, default=1e-5)
    parser.add_argument('--step', type=int, default=16)
    parser.add_argument('--gamma', type=float, default=0.2)
    parser.add_argument('--vis-iter', type=int, default=0)
    parser.add_argument('--vis-maxd', type=int, default=0.031)
    parser.add_argument('--server', type=str, default='http://localhost')
    parser.add_argument('--env', type=str, default='main')
    opt = parser.parse_args()
    opt.train_split = opt.train_split.split(',')
    opt.test_split = opt.test_split.split(',')
    return opt


def train(opt, epoch, train_loader, dpnet, stnet, dpn_optim, stn_optim):
    dpnet = dpnet.train()
    stnet = stnet.train()
    train_len = len(train_loader)

    for iteration, batch in enumerate(train_loader):
        start_time = time.time()
        # rgb_mat: material
        # collection, key, raw_rgb, raw_nir, rgb_exposure, nir_exposureosure, rgb_mat, nir_mat = batch
        collection, key, raw_rgb, raw_nir, rgb_exposure, nir_exposureosure = batch
        # =================================== Preprocessing ====================================
        # black level correction, b=2 is the maximum pixel value when there is no light
        rgb = F.relu((raw_rgb - opt.black_level) / (255.0 - opt.black_level)).cuda()
        nir = F.relu((raw_nir - opt.black_level) / (255.0 - opt.black_level)).cuda()
        # normalization and pixels are clamped
        rgb_ratio = 0.5 / (rgb.mean(1).mean(1).mean(1) + 1e-3)
        nir_ratio = 0.5 / (nir.mean(1).mean(1).mean(1) + 1e-3)
        rgb = torch.clamp(rgb * rgb_ratio.view(-1, 1, 1, 1), 0.0, opt.clamp_value)
        nir = torch.clamp(nir * nir_ratio.view(-1, 1, 1, 1), 0.0, opt.clamp_value)
        # exposure time correction
        rgb_exposure = rgb_exposure.cuda() * rgb_ratio.view(-1, 1)
        nir_exposureosure = nir_exposureosure.cuda() * nir_ratio.view(-1, 1)
        # 曝光时长比
        exp_ratio = (nir_exposureosure / rgb_exposure).view(-1, 1, 1, 1)
        # =====================================================================================
        # Pyramid construction
        rgbs, nirs = pyramid(rgb, anti_aliasing=True), pyramid(nir, anti_aliasing=True)
        # Forward Pass
        ldisps, rdisps = dpnet(rgb, nir)
        is_flipped = (random.randint(0, 1) == 0)
        if is_flipped:
            frgbs = fliplr_pyramid(rgbs)
            transes = stnet(frgbs, exp_ratio)
            transes = fliplr_pyramid(transes)
        else:
            transes = stnet(rgbs, exp_ratio)
        # Postprocessing
        ldisps_detach = detach_pyramid(ldisps)
        rdisps_detach = detach_pyramid(rdisps)
        transes_detach = detach_pyramid(transes)
        wnirs = warp_pyramid(nirs, ldisps, -1)
        wtranses = warp_pyramid(transes_detach, rdisps, 1)
        wnirs_detach = warp_pyramid(nirs, ldisps_detach, -1)
        wtranses_detach = warp_pyramid(transes, rdisps_detach, 1)
        wldisps = warp_pyramid(ldisps, rdisps, 1)
        wrdisps = warp_pyramid(rdisps, ldisps, -1)
        # Compute loss
        dpn_losses = []
        stn_losses = []
        for i in range(4):
            # left-right disparity maps
            l_consist = l1_loss(ldisps[i], wrdisps[i])
            r_consist = l1_loss(rdisps[i], wldisps[i])
            # alignment term
            l_l1 = l1_loss(wnirs[i], transes_detach[i])
            r_l1 = l1_loss(nirs[i], wtranses[i])
            l_ssim = dssim(wnirs[i], transes_detach[i])
            r_ssim = dssim(nirs[i], wtranses[i])
            l_photo = (1 - opt.alpha) * l_l1 + opt.alpha * l_ssim
            r_photo = (1 - opt.alpha) * r_l1 + opt.alpha * r_ssim
            # smoothness term
            ldisp_gradx, ldisp_grady = grad(ldisps[i])
            rdisp_gradx, rdisp_grady = grad(rdisps[i])
            rgb_gradx, rgb_grady = sobel(rgbs[i])
            nir_gradx, nir_grady = sobel(nirs[i])
            l_easmooth = torch.exp(-l1_mean(rgb_gradx) / opt.edge_factor) * ldisp_gradx.abs() + \
                         torch.exp(-l1_mean(rgb_grady) / opt.edge_factor) * ldisp_grady.abs()
            r_easmooth = torch.exp(-l1_mean(nir_gradx) / opt.edge_factor) * rdisp_gradx.abs() + \
                         torch.exp(-l1_mean(nir_grady) / opt.edge_factor) * rdisp_grady.abs()
            dpn_loss = opt.consist * (l_consist + r_consist) + (l_photo + r_photo) + opt.diffuse_smooth * (
                        l_easmooth + r_easmooth)
            dpn_losses.append(dpn_loss)
            stn_loss = l1_loss(wnirs_detach[i], transes[i]) + l1_loss(nirs[i], wtranses_detach[i])
            stn_losses.append(stn_loss)
        dpn_loss = 0.0
        stn_loss = 0.0
        for i in range(4):
            dpn_loss += dpn_losses[i].mean()
            stn_loss += stn_losses[i].mean()
        # Backward pass
        dpnet.zero_grad()
        dpn_loss.backward()
        dpn_optim.step()
        if not opt.no_filt:
            stnet.zero_grad()
            stn_loss.backward()
            stn_optim.step()
        cur_time = time.time()
        dpn_loss_scalar = float(dpn_loss.cpu().detach().numpy())
        stn_loss_scalar = float(stn_loss.cpu().detach().numpy())
        print('{} [{}]({}/{}) Time:{:>4} DPNLoss:{:>4} STNLoss:{:>4}'.format(opt.env, epoch, iteration, train_len, \
                                                                             round((cur_time - start_time), 2), \
                                                                             round(dpn_loss_scalar, 4),
                                                                             round(stn_loss_scalar, 4)))
        break


def test(opt, epoch, test_loader, dpnet):
    dpnet = dpnet.eval()
    ans = [[], [], [], [], [], [], [], []]
    for iteration, batch in enumerate(test_loader):
        collection, key, raw_rgb, raw_nir, _, _ = batch
        rgb = F.relu((raw_rgb - opt.black_level) / (255.0 - opt.black_level)).cuda()
        nir = F.relu((raw_nir - opt.black_level) / (255.0 - opt.black_level)).cuda()
        rgb_ratio = 0.5 / (rgb.mean(1).mean(1).mean(1) + 1e-3)
        nir_ratio = 0.5 / (nir.mean(1).mean(1).mean(1) + 1e-3)
        rgb = torch.clamp(rgb * rgb_ratio.view(-1, 1, 1, 1), 0.0, opt.clamp_value)
        nir = torch.clamp(nir * nir_ratio.view(-1, 1, 1, 1), 0.0, opt.clamp_value)
        ldisps, rdisps = dpnet(rgb, nir)
        for i in range(rgb.shape[0]):
            invd = cpu_np(ldisps[0][i, 0])
            f = open(Path(opt.data_path) / collection[i] / 'Keypoint' / (key[i] + '_Keypoint.txt'), 'r')
            gts = f.readlines()
            f.close()
            for gt in gts:
                x, y, d, c = gt.split()
                x = round(float(x) * 582) - 1
                x = int(max(0, min(582, x)))
                y = round(float(y) * 429) - 1
                y = int(max(0, min(429, y)))
                d = float(d) * 582
                c = int(c)
                p = max(0, invd[y, x] * 582)
                ans[c].append((p - d) * (p - d))
    rmse = []
    for c in range(8):
        rmse.append(pow(sum(ans[c]) / len(ans[c]), 0.5))
    print('Common    Light     Glass     Glossy  Vegetation   Skin    Clothing    Bag       Mean')
    print(round(rmse[0], 4), '  ', round(rmse[1], 4), '  ', round(rmse[2], 4), '  ', round(rmse[3], 4), '  ',
          round(rmse[4], 4), '  ', round(rmse[5], 4), '  ', round(rmse[6], 4), '  ', round(rmse[7], 4), '  ',
          round(sum(rmse) / 8.0, 4))
    print()

if __name__ == '__main__':
    cv2.setNumThreads(0)
    opt = parse_args()
    print(opt)
    Path(opt.ckpt_path).mkdir(parents=True, exist_ok=True)

    train_set = StereoDataset(opt.data_path, opt.list_path, opt.train_split)
    train_loader = DataLoader(dataset=train_set, num_workers=opt.threads, batch_size=opt.batch_size, shuffle=True)
    test_set = StereoDataset(opt.data_path, opt.list_path, opt.test_split)
    test_loader = DataLoader(dataset=test_set, num_workers=opt.threads, batch_size=opt.batch_size, shuffle=False)

    dpnet = DPN(in_shape=(train_set.height, train_set.width))
    stnet = STN(in_shape=(test_set.height, test_set.width), filt=not opt.no_filt)

    dpnet = dpnet.cuda()
    stnet = stnet.cuda()
    dpnet = nn.DataParallel(dpnet, device_ids=[0, 1])
    stnet = nn.DataParallel(stnet, device_ids=[0, 1])

    dpn_optim = optim.Adam(dpnet.parameters(), lr=opt.lr, weight_decay=opt.decay)
    stn_optim = optim.Adam(stnet.parameters(), lr=opt.lr, weight_decay=opt.decay)

    dpn_sched = optim.lr_scheduler.StepLR(dpn_optim, opt.step, gamma=opt.gamma)
    stn_sched = optim.lr_scheduler.StepLR(stn_optim, opt.step, gamma=opt.gamma)

    # vis = Visualizer(server=opt.server, env=opt.env)

    if opt.resume:
        checkpoint = torch.load(opt.resume)
        dpnet.module.load_state_dict(checkpoint['dpnet'])
        stnet.module.load_state_dict(checkpoint['stnet'])
        dpn_optim.load_state_dict(checkpoint['dpn_optim'])
        stn_optim.load_state_dict(checkpoint['stn_optim'])
        dpn_sched.load_state_dict(checkpoint['dpn_sched'])
        stn_sched.load_state_dict(checkpoint['stn_sched'])
        start_epoch = checkpoint['epoch'] + 1
    else:
        start_epoch = 0
        dpnet.apply(dpn_init)
        stnet.apply(stn_init)

    for epoch in range(start_epoch, opt.n_epochs):
        dpn_sched.step()
        stn_sched.step()
        train(opt, epoch, train_loader, dpnet, stnet, dpn_optim, stn_optim)
        test(opt, epoch, test_loader, dpnet)
        torch.save({'epoch': epoch, 'opt': opt, 'dpnet': dpnet.module.state_dict(), 'stnet': stnet.module.state_dict(),
                    'dpn_optim': dpn_optim.state_dict(), 'stn_optim': stn_optim.state_dict(),
                    'dpn_sched': dpn_sched.state_dict(), 'stn_sched': stn_sched.state_dict()},
                    Path(opt.ckpt_path) / (str(epoch) + '.pth'))
