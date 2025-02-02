"""
Author: Xiaoyang Wu
Email: xiaoyang.wu.cs@gmail.com
Point Group
"""

from functools import partial
from collections import OrderedDict

import numpy as np
import torch
import torch.nn as nn

from pointgroup_ops import ballquery_batch_p, bfs_cluster
from ..utils import offset2batch, batch2offset

from ..builder import MODELS, build_model


@MODELS.register_module()
class PointGroup(nn.Module):
    def __init__(self,
                 backbone=None,
                 backbone_out_channels=64,
                 semantic_num_classes=20,
                 semantic_ignore_index=-1,
                 segment_ignore_index=(-1, 0, 1),
                 instance_ignore_index=-1,
                 cluster_thresh=1.5,
                 cluster_closed_points=300,
                 cluster_propose_points=100,
                 cluster_min_points = 50,
                 ):
        super().__init__()
        if backbone is None:
            backbone = dict(
                type="SpUNet-v1m1",
                in_channels=6,
                num_classes=0,
                channels=(32, 64, 128, 256, 256, 128, 96, 96),
                layers=(2, 3, 4, 6, 2, 2, 2, 2)
            )
        else:
            backbone.num_classes = 0
        norm_fn = partial(nn.BatchNorm1d, eps=1e-3, momentum=0.01)
        self.semantic_num_classes = semantic_num_classes
        self.segment_ignore_index = segment_ignore_index
        self.instance_ignore_index = instance_ignore_index
        self.cluster_thresh = cluster_thresh
        self.cluster_closed_points = cluster_closed_points
        self.cluster_propose_points = cluster_propose_points
        self.cluster_min_points = cluster_min_points
        self.backbone = build_model(backbone)
        self.bias_head = nn.Sequential(
            nn.Linear(backbone_out_channels, backbone_out_channels),
            norm_fn(backbone_out_channels),
            nn.ReLU(),
            nn.Linear(backbone_out_channels, 3)
        )
        self.seg_head = nn.Linear(backbone_out_channels, semantic_num_classes)
        self.ce_criteria = torch.nn.CrossEntropyLoss(ignore_index=semantic_ignore_index)

    def forward(self, input_dict):
        coord = input_dict["coord"]
        segment = input_dict["segment"]
        instance = input_dict["instance"]
        instance_center = input_dict["instance_center"]
        bbox = input_dict["bbox"]
        offset = input_dict["offset"]

        feat = self.backbone(input_dict)
        bias_pred = self.bias_head(feat)
        logit_pred = self.seg_head(feat)

        if self.training:
            # compute loss
            seg_loss = self.ce_criteria(logit_pred, segment)

            mask = (instance != self.instance_ignore_index).float()
            bias_gt = instance_center - coord
            bias_dist = torch.sum(torch.abs(bias_pred - bias_gt), dim=-1)
            bias_l1_loss = torch.sum(bias_dist * mask) / (torch.sum(mask) + 1e-8)

            bias_pred_norm = bias_pred / (torch.norm(bias_pred, p=2, dim=1, keepdim=True) + 1e-8)
            bias_gt_norm = bias_gt / (torch.norm(bias_gt, p=2, dim=1, keepdim=True) + 1e-8)
            cosine_similarity = - (bias_pred_norm * bias_gt_norm).sum(-1)
            bias_cosine_loss = torch.sum(cosine_similarity * mask) / (torch.sum(mask) + 1e-8)

            loss = seg_loss + bias_l1_loss + bias_cosine_loss
            return dict(loss=loss, seg_loss=seg_loss, bias_l1_loss=bias_l1_loss, bias_cosine_loss=bias_cosine_loss)
        else:
            center_pred = coord + bias_pred
            segment_pred = torch.max(logit_pred, 1)[1]  # [n]
            # cluster
            mask = ~ torch.concat([(segment_pred == index).unsqueeze(-1)
                                   for index in self.segment_ignore_index], dim=1).sum(-1).bool()

            if mask.sum() == 0:
                proposals_idx = torch.zeros(0).int()
                proposals_offset = torch.zeros(1).int()
            else:
                center_pred_ = center_pred[mask]
                segment_pred_ = segment_pred[mask]

                batch_ = offset2batch(offset)[mask]
                offset_ = nn.ConstantPad1d((1, 0), 0)(batch2offset(batch_))
                idx, start_len = ballquery_batch_p(center_pred_, batch_.int(), offset_.int(),
                                                   self.cluster_thresh, self.cluster_closed_points)
                proposals_idx, proposals_offset = bfs_cluster(segment_pred_.int().cpu(), idx.cpu(),
                                                              start_len.cpu(), self.cluster_min_points)
                proposals_idx[:, 1] = mask.nonzero().view(-1)[proposals_idx[:, 1].long()].int()

            # get proposal
            proposals_pred = torch.zeros((proposals_offset.shape[0] - 1, center_pred.shape[0]), dtype=torch.int)
            proposals_pred[proposals_idx[:, 0].long(), proposals_idx[:, 1].long()] = 1
            instance_pred = segment_pred[proposals_idx[:, 1][proposals_offset[:-1].long()].long()]
            proposals_point_num = proposals_pred.sum(1)
            proposals_mask = (proposals_point_num > self.cluster_propose_points)
            proposals_pred = proposals_pred[proposals_mask]
            instance_pred = instance_pred[proposals_mask]
            instances = []
            for proposal_id in range(len(proposals_pred)):
                segment_ = proposals_pred[proposal_id]
                confidence_ = logit_pred[segment_.bool(), instance_pred[proposal_id]].mean()
                object_ = segment_pred[proposal_id]
                instances.append(dict(confidence=confidence_, object=object_, segment=segment_))
            return dict(instance_pred=instances)
