import torch
from torch.autograd import Variable
from torch.autograd import Function
import torch.nn.functional as F
import torch.nn as nn
from linalg_utils import pdist2, PDist2Order
from collections import namedtuple
import pytorch_utils as pt_utils
from typing import List, Tuple
import tensor_comprehensions as tc
import os.path as osp
from _ext import pointnet2

BASE_DIR = osp.join(osp.abspath(osp.dirname(__file__)), 'tc_autotune')
tc.GlobalDebugInit(['--dump_cuda=true'])


def _tc_wrapper_fn(fn, name):

    def wrapper(*inputs):
        cache_name = name
        for i, inpt in enumerate(inputs):
            sizes = inpt.size()
            for j, s in enumerate(sizes):
                if j != 0:
                    cache_name += '_'
                cache_name += '{}'.format(s)

            if i != len(inputs) - 1:
                cache_name += '-'

        cache_name += '.tc'
        cache_file = osp.join(BASE_DIR, cache_name)

        if not osp.exists(cache_file + '.cuda') and False:
            fn.autotune(*inputs, **tc.autotuner_settings, cache=cache_file)

        return fn(*inputs)

    return wrapper


class RandomDropout(nn.Module):

    def __init__(self, p=0.5, inplace=False):
        super().__init__()
        self.p = p
        self.inplace = inplace

    def forward(self, X):
        theta = torch.Tensor(1).uniform_(0, self.p)[0]
        return pt_utils.feature_dropout_no_scaling(
            X, theta, self.train, self.inplace
        )


class FurthestPointSampling(Function):

    @staticmethod
    def forward(ctx, xyz: torch.Tensor, npoint: int) -> torch.Tensor:
        r"""
        Uses iterative furthest point sampling to select a set of npoint points that have the largest
        minimum distance

        Parameters
        ----------
        xyz : torch.Tensor
            (B, N, 3) tensor where N > npoint
        npoint : int32
            number of points in the sampled set

        Returns
        -------
        torch.Tensor
            (B, npoint) tensor containing the set
        """
        assert xyz.is_contiguous()

        B, N, _ = xyz.size()

        output = torch.cuda.IntTensor(B, npoint)
        temp = torch.cuda.FloatTensor(B, N).fill_(1e10)

        pointnet2.furthest_point_sampling_wrapper(
            B, N, npoint, xyz, temp, output
        )

        return output

    @staticmethod
    def backward(xyz, a=None):
        return None, None


furthest_point_sample = FurthestPointSampling.apply


def _make_gather_points():

    lang = """
        def gather_points(float(B, C, N) points, int32(B, NP) idx) -> (output) {
            output(b, c, np) = points(b, c, idx(b, np))
        }

        def gather_points_grad(float(B, C, N) points, int32(B, NP) idx, float(B, C, NP) grad_out) -> (grad_points) {
            a = idx(b, np)
            grad_points(b, c, a) +=! grad_out(b, c, np)
        }
    """

    fn = tc.define(
        lang,
        training=True,
        name='gather_points',
        backward='gather_points_grad'
    )

    return _tc_wrapper_fn(fn, 'gather_points')


gather_points = _make_gather_points()


class ThreeNN(Function):

    @staticmethod
    def forward(ctx, unknown: torch.Tensor,
                known: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        r"""
            Find the three nearest neighbors of unknown in known
        Parameters
        ----------
        unknown : torch.Tensor
            (B, n, 3) tensor of known points
        known : torch.Tensor
            (B, m, 3) tensor of unknown points

        Returns
        -------
        dist : torch.Tensor
            (B, n, 3) l2 distance to the three nearest neighbors
        idx : torch.Tensor
            (B, n, 3) index of 3 nearest neighbors
        """
        assert unknown.is_contiguous()
        assert known.is_contiguous()

        B, N, _ = unknown.size()
        m = known.size(1)
        dist2 = torch.cuda.FloatTensor(B, N, 3)
        idx = torch.cuda.IntTensor(B, N, 3)

        pointnet2.three_nn_wrapper(B, N, m, unknown, known, dist2, idx)

        return torch.sqrt(dist2), idx

    @staticmethod
    def backward(ctx, a=None, b=None):
        return None, None


three_nn = ThreeNN.apply



class ThreeInterpolate(Function):

    @staticmethod
    def forward(
            ctx, points: torch.Tensor, idx: torch.Tensor, weight: torch.Tensor
    ) -> torch.Tensor:
        r"""
            Performs weight linear interpolation on 3 points
        Parameters
        ----------
        points : torch.Tensor
            (B, c, m)  Points to be interpolated from
        idx : torch.Tensor
            (B, n, 3) three nearest neighbors of the target points in points
        weight : torch.Tensor
            (B, n, 3) weights

        Returns
        -------
        torch.Tensor
            (B, c, n) tensor of the interpolated points
        """
        assert points.is_contiguous()
        assert idx.is_contiguous()
        assert weight.is_contiguous()

        B, c, m = points.size()
        n = idx.size(1)

        ctx.three_interpolate_for_backward = (idx, weight, m)

        output = torch.cuda.FloatTensor(B, c, n)

        pointnet2.three_interpolate_wrapper(
            B, c, m, n, points, idx, weight, output
        )

        return output

    @staticmethod
    def backward(ctx, grad_out: torch.Tensor
                ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        r"""
        Parameters
        ----------
        grad_out : torch.Tensor
            (B, c, n) tensor with gradients of ouputs

        Returns
        -------
        grad_points : torch.Tensor
            (B, c, m) tensor with gradients of points

        None

        None
        """
        idx, weight, m = ctx.three_interpolate_for_backward
        B, c, n = grad_out.size()

        grad_points = Variable(torch.cuda.FloatTensor(B, c, m).zero_())

        grad_out_data = grad_out.data.contiguous()
        pointnet2.three_interpolate_grad_wrapper(
            B, c, n, m, grad_out_data, idx, weight, grad_points.data
        )

        return grad_points, None, None


three_interpolate = ThreeInterpolate.apply


def _make_group_points():
    lang = """
        def group_points(float(B, C, N) points, int32(B, NP, NS) idx) -> (output) {
            output(b, c, np, ns) = points(b, c, idx(b, np, ns))
        }

        def group_points_grad(float(B, C, N) points, int32(B, NP, NS) idx, float(B, C, NP, NS) grad_out) -> (grad_points) {
            grad_points(b, c, idx(b, np, ns)) +=! grad_out(b, c, np, ns)
        }
    """

    fn = tc.define(
        lang,
        training=True,
        name='group_points',
        backward='group_points_grad'
    )

    return _tc_wrapper_fn(fn, 'group_points')


group_points = _make_group_points()


class BallQuery(Function):

    @staticmethod
    def forward(
            ctx, radius: float, nsample: int, xyz: torch.Tensor,
            new_xyz: torch.Tensor
    ) -> torch.Tensor:
        r"""

        Parameters
        ----------
        radius : float
            radius of the balls
        nsample : int
            maximum number of points in the balls
        xyz : torch.Tensor
            (B, N, 3) xyz coordinates of the points
        new_xyz : torch.Tensor
            (B, npoint, 3) centers of the ball query

        Returns
        -------
        torch.Tensor
            (B, npoint, nsample) tensor with the indicies of the points that form the query balls
        """
        assert new_xyz.is_contiguous()
        assert xyz.is_contiguous()

        B, N, _ = xyz.size()
        npoint = new_xyz.size(1)
        idx = torch.cuda.IntTensor(B, npoint, nsample).zero_()

        pointnet2.ball_query_wrapper(
            B, N, npoint, radius, nsample, new_xyz, xyz, idx
        )

        return idx

    @staticmethod
    def backward(ctx, a=None):
        return None, None, None, None


ball_query = BallQuery.apply


class QueryAndGroup(nn.Module):
    r"""
    Groups with a ball query of radius

    Parameters
    ---------
    radius : float32
        Radius of ball
    nsample : int32
        Maximum number of points to gather in the ball
    """

    def __init__(self, radius: float, nsample: int, use_xyz: bool = True):
        super().__init__()
        self.radius, self.nsample, self.use_xyz = radius, nsample, use_xyz

    def forward(
            self,
            xyz: torch.Tensor,
            new_xyz: torch.Tensor,
            points: torch.Tensor = None
    ) -> Tuple[torch.Tensor]:
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            xyz coordinates of the points (B, N, 3)
        new_xyz : torch.Tensor
            centriods (B, npoint, 3)
        points : torch.Tensor
            Descriptors of the points (B, C, N)

        Returns
        -------
        new_points : torch.Tensor
            (B, 3 + C, npoint, nsample) tensor
        """

        idx = ball_query(self.radius, self.nsample, xyz, new_xyz)
        xyz_trans = xyz.transpose(1, 2).contiguous()
        grouped_xyz = group_points(xyz_trans, idx)  # (B, 3, npoint, nsample)
        grouped_xyz -= new_xyz.transpose(1, 2).unsqueeze(-1)

        if points is not None:
            grouped_points = group_points(points, idx)
            if self.use_xyz:
                new_points = torch.cat([grouped_xyz, grouped_points],
                                       dim=1)  # (B, C + 3, npoint, nsample)
            else:
                new_points = group_points
        else:
            new_points = grouped_xyz

        return new_points


class GroupAll(nn.Module):
    r"""
    Groups all points

    Parameters
    ---------
    """

    def __init__(self, use_xyz: bool = True):
        super().__init__()
        self.use_xyz = use_xyz

    def forward(
            self,
            xyz: torch.Tensor,
            new_xyz: torch.Tensor,
            points: torch.Tensor = None
    ) -> Tuple[torch.Tensor]:
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            xyz coordinates of the points (B, N, 3)
        new_xyz : torch.Tensor
            Ignored
        points : torch.Tensor
            Descriptors of the points (B, C, N)

        Returns
        -------
        new_points : torch.Tensor
            (B, C + 3, 1, N) tensor
        """

        grouped_xyz = xyz.transpose(1, 2).unsqueeze(2)
        if points is not None:
            grouped_points = points.unsqueeze(2)
            if self.use_xyz:
                new_points = torch.cat([grouped_xyz, grouped_points],
                                       dim=1)  # (B, 3 + C, 1, N)
            else:
                new_points = group_points
        else:
            new_points = grouped_xyz

        return new_points
