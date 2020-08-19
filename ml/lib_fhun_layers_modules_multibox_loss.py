# -*- coding: utf-8 -*-
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from lib_fhun_utils_box_utils import match, log_sum_exp


class MultiBoxLoss(nn.Module):
    """SSD Weighted Loss Function
    Compute Targets:
        1) Produce Confidence Target Indices by matching  ground truth boxes
           with (default) 'priorboxes' that have jaccard index > threshold parameter
           (default threshold: 0.5).
        2) Produce localization target by 'encoding' variance into offsets of ground
           truth boxes and their matched  'priorboxes'.
        3) Hard negative mining to filter the excessive number of negative examples
           that comes with using a large number of default bounding boxes.
           (default negative:positive ratio 3:1)
    Objective Loss:
        L(x,c,l,g) = (Lconf(x, c) + αLloc(x,l,g)) / N
        Where, Lconf is the CrossEntropy Loss and Lloc is the SmoothL1 Loss
        weighted by α which is set to 1 by cross val.
        Args:
            c: class confidences,
            l: predicted boxes,
            g: ground truth boxes
            N: number of matched default boxes
        See: https://arxiv.org/pdf/1512.02325.pdf for more details.
    """

    def __init__(self, cfg, priors, use_gpu=True):
        super(MultiBoxLoss, self).__init__()
        self.use_gpu = use_gpu
        self.num_classes = cfg.NUM_CLASSES
        self.background_label = cfg.BACKGROUND_LABEL
        self.negpos_ratio = cfg.NEGPOS_RATIO
        self.threshold = cfg.MATCHED_THRESHOLD
        self.unmatched_threshold = cfg.UNMATCHED_THRESHOLD
        self.variance = cfg.VARIANCE
        self.priors = priors

    def forward(self, predictions, targets):
        """Multibox Loss
        Args:
            predictions (tuple): A tuple containing loc preds, conf preds,
            and prior boxes from SSD net.
                conf shape: torch.size(batch_size,num_priors,num_classes)
                loc shape: torch.size(batch_size,num_priors,4)
                priors shape: torch.size(num_priors,4)
            ground_truth (tensor): Ground truth boxes and labels for a batch,
                shape: [batch_size,num_objs,5] (last idx is the label).
        """

        # predictions: outputs from model
        # loc_data.shape      = {batch_size, 11620, 4}
        # conf_data.shape     = {batch_size, 11620, n_classes}
        # occluded_data.shape = {batch_size, 11620, 1}
        loc_data, conf_data, occluded_data = predictions
        # color_data = color_data[:,:,:3]      # reduce to 3 for RGB channels

        num = loc_data.size(0)
        priors = self.priors
        # priors = priors[:loc_data.size(1), :]
        num_priors = (priors.size(0))
        num_classes = self.num_classes

        # targets: desired groundtruths
        # {bbox_x1, bbox_y1, bbox_x2, bbox_y2, class_names, occluded_iou, co1, co2, co3}

        # match priors (default boxes) and ground truth boxes
        loc_t = torch.Tensor(num, num_priors, 4)
        conf_t = torch.LongTensor(num, num_priors)
        occluded_t = torch.Tensor(num, num_priors, 1)
        corner_t = torch.Tensor(num, num_priors, 16)
        for idx in range(num):
            truths = targets[idx][:,:4].data
            labels = targets[idx][:,4].data
            ious = targets[idx][:,5:6].data
            defaults = priors.data
            match(self.threshold, truths, defaults, self.variance, labels, ious,
                  loc_t, conf_t, occluded_t, idx)
        if self.use_gpu:
            loc_t = loc_t.cuda()
            conf_t = conf_t.cuda()
            occluded_t = occluded_t.cuda()

        # wrap targets
        loc_t = Variable(loc_t, requires_grad=False)
        conf_t = Variable(conf_t, requires_grad=False)
        occluded_t = Variable(occluded_t, requires_grad=False)

        pos = conf_t > 0
        # num_pos = pos.sum()

        # Localization Loss (Smooth L1)
        # Shape: [batch,num_priors,4]
        pos_idx = pos.unsqueeze(pos.dim()).expand_as(loc_data)
        loc_p = loc_data[pos_idx].view(-1,4)
        loc_t = loc_t[pos_idx].view(-1,4)
        loss_l = F.smooth_l1_loss(loc_p, loc_t, size_average=False)

        iou_idx = pos.unsqueeze(pos.dim()).expand_as(occluded_data)
        iou_p = occluded_data[iou_idx].view(-1,1)
        iou_t = occluded_t[iou_idx].view(-1,1)
        iou_p[iou_t == -1] = -1
        loss_o = F.mse_loss(iou_p, iou_t, size_average=False)

        # Compute max conf across batch for hard negative mining
        batch_conf = conf_data.view(-1, self.num_classes)
        loss_c = log_sum_exp(batch_conf) - batch_conf.gather(1, conf_t.view(-1,1))

        # Hard Negative Mining
        loss_c = loss_c.view(num, -1)
        loss_c[pos] = 0  # filter out pos boxes for now
        _, loss_idx = loss_c.sort(1, descending=True)
        _, idx_rank = loss_idx.sort(1)
        num_pos = pos.long().sum(1, keepdim=True)
        num_neg = torch.clamp(self.negpos_ratio*num_pos, max=pos.size(1)-1)
        neg = idx_rank < num_neg.expand_as(idx_rank)

        # Confidence Loss Including Positive and Negative Examples
        pos_idx = pos.unsqueeze(2).expand_as(conf_data)
        neg_idx = neg.unsqueeze(2).expand_as(conf_data)
        conf_p = conf_data[(pos_idx+neg_idx).gt(0)].view(-1, self.num_classes)
        targets_weighted = conf_t[(pos+neg).gt(0)]
        loss_c = F.cross_entropy(conf_p, targets_weighted, size_average=False)

        # Sum of losses: L(x,c,l,g) = (Lconf(x, c) + αLloc(x,l,g)) / N

        N = num_pos.data.sum()

        N = N.double()
        loss_l = loss_l.double()
        loss_c = loss_c.double()
        loss_o = loss_o.double()
        
        loss_l /= N
        loss_c /= N
        loss_o /= N
        return loss_l, loss_c, loss_o