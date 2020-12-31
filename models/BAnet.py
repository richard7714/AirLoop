#!/usr/bin/env python3

import math
import torch
import numpy as np
import kornia as kn
import torch.nn as nn
import torch.nn.functional as F

import models


class BAGDnet(nn.Module):
    '''
    Args:
        K: stands for the camera intrinsic
        MPs: the map points in the memory R3. N * 4 the first element is an unique assigned id.
        KFs: the Translation matrix for the camera pose SE3. N * 1 the first element is an unique assigned id.
    Parameters:
        QuatsLog: the log quaternion of the rotation
        CameraPosition: the position of the camera (x, y, z)
        Landmarks: the landmarks for observation (x, y, z)
        Tcw: the transform matrix of the camera pose
    '''
    def __init__(self, MPs, KFs, K):
        super().__init__()
        self.K = K
        self.Tcw = KFs[:,1:].view(-1,4,4)

        self.Landmarks = nn.Parameter(MPs[:,1:])#+torch.randn_like(MPs[:,1:])*0.1)
        self.CameraPosition = nn.Parameter(self.Tcw[:,:,3][:,:3])

        quats = kn.rotation_matrix_to_quaternion(self.Tcw[:,:3,:3].contiguous())
        self.QuatsLog = nn.Parameter(kn.quaternion_exp_to_log(quats))

        # indexing the matches of key frames and Map Point
        self.idxMP = MPs[:,0].type(torch.int)
        self.idxKF = KFs[:,0].type(torch.int)
        self.LandmarksHomo = kn.convert_points_to_homogeneous(self.Landmarks)

    def forward(self, frame_id, point_id):
        indexKF = torch.where(frame_id==self.idxKF)[1]
        indexMP = torch.where(point_id==self.idxMP)[1]

        rots_matrix = kn.quaternion_to_rotation_matrix(kn.quaternion_log_to_exp(self.QuatsLog))
        trans_matrix = F.pad(input=rots_matrix, pad=(0,0,0,1), mode='constant', value=0)
        CameraPositionHomo = kn.convert_points_to_homogeneous(self.CameraPosition)
        self.trans_matrix = torch.cat((trans_matrix, CameraPositionHomo.unsqueeze(2)), 2)

        points = (self.trans_matrix[indexKF] @ self.LandmarksHomo[indexMP].unsqueeze(-1)).squeeze(-1)
        Pc = kn.convert_points_from_homogeneous(points)
        return kn.project_points(Pc, self.K)

class ConsecutiveMatch(nn.Module):
    def __init__(self):
        super().__init__()
        self.cosine = models.PairwiseCosine()

    def forward(self, desc_src, desc_dst, points_dst):
        confidence, idx = self.cosine(desc_src, desc_dst).max(dim=2)
        matched = points_dst.gather(1, idx.unsqueeze(2).expand(-1, -1, 2))

        return matched, confidence


if __name__ == "__main__":
    '''Test codes'''
    import time, math, torch, argparse
    from tool import EarlyStopScheduler
    from optim import LevenbergMarquardt
    from optim import UpDownDampingScheduler

    parser = argparse.ArgumentParser(description='Test BAGD')
    parser.add_argument("--device", type=str, default='cuda', help="cuda, cuda:0, or cpu")
    parser.add_argument('--seed', type=int, default=0, help='Random seed.')
    parser.add_argument("--optim", type=str, default='SGD', help="LM or SGD")
    parser.add_argument('--damping', type=float, default=2, help='damping')
    parser.add_argument("--max-block", type=int, default=1000, help="max block size")
    parser.add_argument('--lr', type=float, default=1e-3, help='Random seed.')
    parser.add_argument('--min-lr', type=float, default=1e-4, help='Random seed.')
    parser.add_argument("--factor", type=float, default=0.1, help="factor of lr")
    parser.add_argument("--patience", type=int, default=5, help="training patience")

    args = parser.parse_args()
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    MPs = torch.from_numpy(np.loadtxt("data/BAtest/MP.txt")).cuda()
    KFs = torch.from_numpy(np.loadtxt("data/BAtest/KF.txt")).cuda()
    Matches = torch.from_numpy(np.loadtxt("data/BAtest/Match.txt")).cuda()

    fx, fy, cx, cy = 320, 320, 320, 240
    affine = torch.FloatTensor([[[fx, 0, cx], [0, fy, cy]]])
    K = kn.convert_affinematrix_to_homography(affine).cuda()

    net = BAGDnet(MPs, KFs, K)

    SmoothLoss = nn.SmoothL1Loss(beta = math.sqrt(5.99))
    if args.optim == 'LM':
        optimizer = LevenbergMarquardt(net.parameters(), damping=args.damping, max_block=args.max_block)
        scheduler = UpDownDampingScheduler(optimizer, 2, True)
    elif args.optim == 'SGD':
        optimizer = torch.optim.SGD(net.parameters(), lr=1e-5)
        scheduler = EarlyStopScheduler(optimizer, args.factor, args.patience, args.min_lr, True)

    pixel = Matches[:,2:4]
    frame_id = Matches[:,0,None].type(torch.int)
    point_id = Matches[:,1,None].type(torch.int)

    for i in range(200):
        output = net(frame_id, point_id)
        loss = SmoothLoss(output, pixel)
        loss.backward()
        if args.optim == 'LM':
            optimizer.step(loss)
            scheduler.step()
        elif args.optim == 'SGD':
            optimizer.step()
            if scheduler.step(loss):
                break
        print('Epoch: %d, Loss: %.7f'%(i, loss))
