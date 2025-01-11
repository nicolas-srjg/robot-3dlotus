from scipy.spatial.transform import Rotation as R
import collections

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import einops

from genrobo3d.utils.rotation_transform import discrete_euler_to_quaternion
from genrobo3d.models.base import BaseModel, RobotPoseEmbedding
from genrobo3d.utils.rotation_transform import RotationMatrixTransform
from genrobo3d.models.PointTransformerV3.model import (
    PointTransformerV3, offset2bincount, offset2batch
)
from genrobo3d.models.PointTransformerV3.model_ca import PointTransformerV3CA
from genrobo3d.utils.action_position_utils import get_best_pos_from_disc_pos


class ActionHead(nn.Module):
    def __init__(
        self, reduce, pos_pred_type, rot_pred_type, hidden_size, dim_actions, max_traj_len,
        dropout=0, voxel_size=0.01, euler_resolution=5, ptv3_config=None, pos_bins=50,
        traj_embed_size=64,
    ) -> None:
        super().__init__()
        assert reduce in ['max', 'mean', 'attn']
        assert pos_pred_type in ['heatmap_mlp', 'heatmap_disc']
        assert rot_pred_type in ['quat', 'rot6d', 'euler', 'euler_disc']

        self.reduce = reduce
        self.pos_pred_type = pos_pred_type
        self.rot_pred_type = rot_pred_type
        self.hidden_size = hidden_size
        self.dim_actions = dim_actions
        self.voxel_size = voxel_size
        self.euler_resolution = euler_resolution
        self.euler_bins = 360 // euler_resolution
        self.pos_bins = pos_bins
        self.max_traj_len = max_traj_len

        if traj_embed_size > 0:
            self.traj_embedding = nn.Embedding(max_traj_len, traj_embed_size)
        else:
            assert max_traj_len == 1
            self.traj_embedding = None

        if self.pos_pred_type == 'heatmap_disc':
            self.heatmap_mlp = nn.Sequential(
                nn.Linear(hidden_size + traj_embed_size, hidden_size),
                nn.LeakyReLU(0.02),
                nn.Dropout(dropout),
                nn.Linear(hidden_size, 3 * self.pos_bins * 2)
            )
        else:
            output_size = 1 + 3
            self.heatmap_mlp = nn.Sequential(
                nn.Linear(hidden_size + traj_embed_size, hidden_size),
                nn.LeakyReLU(0.02),
                nn.Dropout(dropout),
                nn.Linear(hidden_size, output_size)
            )

        if self.rot_pred_type == 'euler_disc':
            output_size = self.euler_bins * 3 + 1
        else:
            output_size = dim_actions - 3
        if self.reduce == 'attn':
            output_size += 1
        self.action_mlp = nn.Sequential(
            nn.Linear(hidden_size + traj_embed_size, hidden_size),
            nn.LeakyReLU(0.02),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, output_size + 1) # need to predict stop
        )
        
    def forward(
        self, point_embeds, npoints_in_batch, coords=None, temp=1, dec_layers_embed=None,
    ):
        '''
        Args:
            point_embeds: (# all points, dim)
            npoints_in_batch: (batch_size, )
            coords: (# all points, 3)
        Return:
            pred_actions: (batch, max_traj_len, dim_actions)
        ''' 
        device = point_embeds.device
        point_embeds = point_embeds.unsqueeze(1).expand(-1, self.max_traj_len, -1)

        if self.traj_embedding is not None:
            traj_embeds = self.traj_embedding(torch.arange(self.max_traj_len).to(device))
            # (#points, max_traj_len, dim)
            point_embeds = torch.cat(
                [point_embeds, traj_embeds.expand(point_embeds.size(0), -1, -1)], -1
            )

        if self.pos_pred_type.startswith('heatmap_mlp'):
            # (#npoints, max_traj_len, 4)
            heatmap_embeds = self.heatmap_mlp(point_embeds)
            heatmaps = torch.split(heatmap_embeds[..., 0], npoints_in_batch)
            new_coords = coords.unsqueeze(1) + heatmap_embeds[..., 1:]
            heatmaps = [torch.softmax(x / temp, dim=0)for x in heatmaps]
            # print([x.sum() for x in heatmaps], [x.size() for x in heatmaps])
            # print(npoints_in_batch, temp, [x.max() for x in heatmaps], [x.min() for x in heatmaps])
            new_coords = torch.split(new_coords, npoints_in_batch)
            xt = torch.stack([
                torch.einsum('pt,ptc->tc', h, p) for h, p in zip(heatmaps, new_coords)
            ], dim=0)
            
        elif self.pos_pred_type == 'heatmap_disc':
            xt = self.heatmap_mlp(point_embeds) # (npoints, max_traj_len, 3*pos_bins*2)
            xt = einops.rearrange(xt, 'n t (c b) -> t c n b', c=3) # (t, 3, #npoints, pos_bins*2)

        if self.reduce == 'max':
            split_point_embeds = torch.split(point_embeds, npoints_in_batch)
            pc_embeds = torch.stack([torch.max(x, 0)[0] for x in split_point_embeds], 0)
            # (batch_size, max_traj_len, dim)
            action_embeds = self.action_mlp(pc_embeds)
        elif self.reduce == 'mean':
            split_point_embeds = torch.split(point_embeds, npoints_in_batch)
            pc_embeds = torch.stack([torch.mean(x, 0) for x in split_point_embeds], 0)
            action_embeds = self.action_mlp(pc_embeds)
        else: # attn
            action_embeds = self.action_mlp(point_embeds)
            action_heatmaps = torch.split(action_embeds[:, :1], npoints_in_batch)
            action_heatmaps = [torch.softmax(x / temp, dim=0)for x in action_heatmaps]
            split_action_embeds = torch.split(action_embeds[:, 1:], npoints_in_batch)
            action_embeds = torch.stack([(h*v).sum(dim=0) for h, v in zip(action_heatmaps, split_action_embeds)], 0)
            
        # (batch_size, max_traj, dim)
        if self.rot_pred_type == 'quat':
            xr = action_embeds[..., :4]
            xr = xr / xr.square().sum(dim=-1, keepdim=True).sqrt()
        elif self.rot_pred_type == 'rot6d':
            xr = action_embeds[..., :6]
        elif self.rot_pred_type in ['euler', 'euler_delta']:
            xr = action_embeds[..., :3]
        elif self.rot_pred_type == 'euler_disc':
            xr = action_embeds[..., :self.euler_bins*3]
            # (batch_size, max_traj_len, euler_bins, 3)
            xr = einops.rearrange(xr, 'n t (b c) -> n t b c', c=3)
        
        # (batch_size, max_traj_len)
        xo = action_embeds[..., -2]
        xstop = action_embeds[..., -1]
        
        return xt, xr, xo, xstop


class MotionPlannerPTV3AdaNorm(BaseModel):
    """Adaptive batch/layer normalization conditioned on text/pose/stepid
    """
    def __init__(self, config):
        super().__init__()

        config.defrost()
        config.ptv3_config.pdnorm_only_decoder = config.ptv3_config.get('pdnorm_only_decoder', False)
        config.ptv3_config.in_channels += config.action_config.pc_label_channels
        config.freeze()
        
        self.config = config
        self.ptv3_model = PointTransformerV3(**config.ptv3_config)

        # 0: obstacle, 1: robot, 2: object, 3: target
        self.pc_label_embedding = nn.Embedding(4, config.action_config.pc_label_channels)

        act_cfg = config.action_config
        self.txt_fc = nn.Linear(act_cfg.txt_ft_size, act_cfg.context_channels)
        if act_cfg.txt_reduce == 'attn':
            self.txt_attn_fc = nn.Linear(act_cfg.txt_ft_size, 1)
        if act_cfg.use_ee_pose:
            self.pose_embedding = RobotPoseEmbedding(act_cfg.context_channels)
        # if act_cfg.use_step_id:
        #     self.stepid_embedding = nn.Embedding(act_cfg.max_steps, act_cfg.context_channels)

        # act_head_type = self.config.action_config.get('act_head', 0)
        self.act_proj_head = ActionHead(
            act_cfg.reduce, act_cfg.pos_pred_type, act_cfg.rot_pred_type, 
            config.ptv3_config.dec_channels[0], act_cfg.dim_actions, act_cfg.max_traj_len,
            dropout=act_cfg.dropout, voxel_size=act_cfg.voxel_size,
            ptv3_config=config.ptv3_config, pos_bins=config.action_config.pos_bins,
            traj_embed_size=config.action_config.traj_embed_size
        )
           
        self.apply(self._init_weights)

        self.rot_transform = RotationMatrixTransform()

    def prepare_ptv3_batch(self, batch):
        outs = {
            'coord': batch['pc_fts'][:, :3],
            'grid_size': self.config.action_config.voxel_size,
            'offset': batch['offset'],
            'batch': offset2batch(batch['offset']),
            'feat': batch['pc_fts'],
        }
        pc_label_embeds = (self.pc_label_embedding(torch.LongTensor([0, 1, 2, 3],device=batch['pc_labels'].device))[None,None,:]*batch['pc_labels']).sum(dim=1)
        #pc_label_embeds = self.pc_label_embedding(batch['pc_labels'])
        outs['feat'] = torch.cat([outs['feat'], pc_label_embeds], dim=-1)

        # encode context for each point cloud
        ctx_embeds = self.txt_fc(batch['txt_embeds'])
        if self.config.action_config.txt_reduce == 'attn':
            txt_weights = torch.split(self.txt_attn_fc(batch['txt_embeds']), batch['txt_lens'])
            txt_embeds = torch.split(ctx_embeds, batch['txt_lens'])
            ctx_embeds = []
            for txt_weight, txt_embed in zip(txt_weights, txt_embeds):
                txt_weight = torch.softmax(txt_weight, 0)
                ctx_embeds.append(torch.sum(txt_weight * txt_embed, 0))
            ctx_embeds = torch.stack(ctx_embeds, 0) 
        
        if self.config.action_config.use_ee_pose:
            pose_embeds = self.pose_embedding(batch['ee_poses'])
            ctx_embeds += pose_embeds

        # if self.config.action_config.use_step_id:
        #     step_embeds = self.stepid_embedding(batch['step_ids'])
        #     ctx_embeds += step_embeds

        outs['context'] = ctx_embeds

        return outs

    def forward(self, batch, compute_loss=False, **kwargs):
        '''batch data:
            pc_fts: (batch, npoints, dim)
            txt_embeds: (batch, txt_dim)
        '''
        batch = self.prepare_batch(batch)
        device = batch['pc_fts'].device

        ptv3_batch = self.prepare_ptv3_batch(batch)

        point_outs = self.ptv3_model(ptv3_batch, return_dec_layers=True)

        pred_actions = self.act_proj_head(
            point_outs[-1].feat, batch['npoints_in_batch'], coords=point_outs[-1].coord,
            temp=self.config.action_config.get('pos_heatmap_temp', 1),
            # dec_layers_embed=[point_outs[k] for k in [0, 1, 2, 3, 4]] if self.config.ptv3_config.dec_depths[0] == 1 else [point_outs[k] for k in [0, 2, 4, 6, 8]] # TODO
        )
            
        pred_pos, pred_rot, pred_open, pred_stop = pred_actions
        if self.config.action_config.pos_pred_type == 'heatmap_disc':
            if kwargs.get('compute_final_action', True):
                # import time
                # st = time.time()
                cont_pred_pos = []
                npoints_in_batch = offset2bincount(point_outs[-1].offset).data.cpu().numpy().tolist()
                # [(max_traj_len, 3, npoints, pos_bins)]
                split_pred_pos = torch.split(pred_pos, npoints_in_batch, dim=2)
                # [(npoints, 3)]
                split_coords = torch.split(point_outs[-1].coord, npoints_in_batch)
                for i in range(len(npoints_in_batch)):
                    cont_pred_pos.append([])
                    for j in range(self.config.action_config.max_traj_len):
                        disc_pos_prob = torch.softmax(
                            split_pred_pos[i][j].reshape(3, -1), dim=-1
                        )
                        cont_pred_pos[-1].append(
                            get_best_pos_from_disc_pos(
                                disc_pos_prob.data.cpu().numpy(), 
                                split_coords[i].data.cpu().numpy(), 
                                best=self.config.action_config.get('best_disc_pos', 'max'),
                                topk=split_coords[i].size(1) * 10,
                                pos_bin_size=self.config.action_config.pos_bin_size, 
                                pos_bins=self.config.action_config.pos_bins, 
                            )
                        )
                # (batch_size, max_traj_len, 3)
                cont_pred_pos = torch.from_numpy(np.array(cont_pred_pos)).float().to(device)
                # print('time', time.time() - st)
                pred_pos = cont_pred_pos
            else:
                pred_pos = batch['gt_trajs'][..., :3]

        batch_size, max_traj_len = pred_rot.size()[:2]
        new_rot_size = [batch_size*max_traj_len] + list(pred_rot.size()[2:])
        pred_rot = pred_rot.reshape(*new_rot_size)
        if self.config.action_config.rot_pred_type == 'rot6d':
            # no grad
            pred_rot = self.rot_transform.matrix_to_quaternion(
                self.rot_transform.compute_rotation_matrix_from_ortho6d(pred_rot.data.cpu())
            ).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler':
            pred_rot = pred_rot * 180
            pred_rot = self.rot_transform.euler_to_quaternion(pred_rot.data.cpu()).float().to(device)
        elif self.config.action_config.rot_pred_type == 'euler_disc':
            pred_rot = torch.argmax(pred_rot, 1).data.cpu().numpy()
            pred_rot = np.stack([discrete_euler_to_quaternion(x, self.act_proj_head.euler_resolution) for x in pred_rot], 0)
            pred_rot = torch.from_numpy(pred_rot).to(device)
        pred_rot = pred_rot.reshape(batch_size, max_traj_len, -1)
        final_pred_actions = torch.cat(
            [pred_pos, pred_rot, pred_open.unsqueeze(-1), pred_stop.unsqueeze(-1)], dim=-1
        )
        
        if compute_loss:
            losses = self.compute_loss(
                pred_actions, batch['gt_trajs'], batch['gt_trajs_stop'],
                disc_pos_probs=batch.get('gt_trajs_disc_pos_probs', None), 
                npoints_in_batch=batch['npoints_in_batch'],
                tgt_traj_masks=batch['traj_masks'],
            )
            return final_pred_actions, losses
        else:
            return final_pred_actions

    def compute_loss(
        self, pred_actions, tgt_actions, tgt_stops, 
        disc_pos_probs=None, npoints_in_batch=None, tgt_traj_masks=None
    ):
        """
        Args:
            pred_actions: (batch_size, max_traj_len, dim_action)
            tgt_actions: (batch_size, max_traj_len, dim_action)
            tgt_stops: (batch, max_traj_len)
            disc_pos_probs: (max_traj_len, 3, #all points, pos_bins)
            tgt_traj_masks: (batch, max_traj_len)
        """
        # loss_cfg = self.config.loss_config
        batch_size = tgt_actions.size(0)
        device = tgt_actions.device
        
        pred_pos, pred_rot, pred_open, pred_stop = pred_actions
        tgt_pos, tgt_rot, tgt_open = tgt_actions[..., :3], tgt_actions[..., 3:-1], tgt_actions[..., -1]

        # position loss
        if self.config.action_config.pos_pred_type == 'heatmap_disc':
            # [(max_traj_len, 3, #npoints, pos_bins)]
            split_pred_pos = torch.split(pred_pos, npoints_in_batch, dim=2)
            pos_loss = 0
            for i in range(len(npoints_in_batch)):
                mask = tgt_traj_masks[i].unsqueeze(1).expand(-1, 3).reshape(-1)
                pos_loss += torch.sum(F.cross_entropy(
                    einops.rearrange(split_pred_pos[i], 't c n b -> (t c) (n b)'), 
                    einops.rearrange(disc_pos_probs[i].to(device), 't c b -> (t c) b'), 
                    reduction='none'
                ) * mask) / mask.sum()
            pos_loss /= len(npoints_in_batch)
        else:
            pos_loss = torch.sum(
                F.mse_loss(pred_pos, tgt_pos, reduction='none') * tgt_traj_masks.unsqueeze(-1)
            ) / tgt_traj_masks.sum() / 3

        # rotation loss
        if self.config.action_config.rot_pred_type == 'quat':
            # Automatically matching the closest quaternions (symmetrical solution)
            tgt_rot_ = -tgt_rot.clone()
            rot_loss = F.mse_loss(pred_rot, tgt_rot, reduction='none').mean(-1)
            rot_loss_ = F.mse_loss(pred_rot, tgt_rot_, reduction='none').mean(-1)
            select_mask = (rot_loss < rot_loss_).float()
            rot_loss = (select_mask * rot_loss + (1 - select_mask) * rot_loss_).mean()
        elif self.config.action_config.rot_pred_type == 'rot6d':
            tgt_rot6d = self.rot_transform.get_ortho6d_from_rotation_matrix(
                self.rot_transform.quaternion_to_matrix(tgt_rot.data.cpu())
            ).float().to(device)
            rot_loss = F.mse_loss(pred_rot, tgt_rot6d)
        elif self.config.action_config.rot_pred_type == 'euler':
            # Automatically matching the closest angles
            tgt_rot_ = tgt_rot.clone()
            tgt_rot_[tgt_rot < 0] += 2
            tgt_rot_[tgt_rot > 0] -= 2
            rot_loss = F.mse_loss(pred_rot, tgt_rot, reduction='none')
            rot_loss_ = F.mse_loss(pred_rot, tgt_rot_, reduction='none')
            select_mask = (rot_loss < rot_loss_).float()
            rot_loss = (select_mask * rot_loss + (1 - select_mask) * rot_loss_).mean()
        elif self.config.action_config.rot_pred_type == 'euler_disc':
            tgt_rot = tgt_rot.long()    # (batch_size, traj_len, 3)
            rot_loss = F.cross_entropy(
                einops.rearrange(pred_rot, 'n t d c -> (n t c) d'), 
                einops.rearrange(tgt_rot, 'n t c -> (n t c)'), 
                reduction='none'
            )
            rot_loss = einops.rearrange(rot_loss, '(n t c) -> n t c', c=3, n=batch_size) 
            rot_loss = torch.sum(rot_loss * tgt_traj_masks.unsqueeze(-1)) / tgt_traj_masks.sum() / 3
        else: # euler_delta
            rot_loss = F.mse_loss(pred_rot, tgt_rot)
            
        # openness state loss
        open_loss = F.binary_cross_entropy_with_logits(
            pred_open, tgt_open, reduction='none'
        ) 
        open_loss = (open_loss * tgt_traj_masks).sum() / tgt_traj_masks.sum()

        # stop loss
        stop_loss = F.binary_cross_entropy_with_logits(
            pred_stop, tgt_stops.float(), reduction='none'
        )
        stop_loss = (stop_loss * tgt_traj_masks).sum() / tgt_traj_masks.sum()

        total_loss = self.config.loss_config.pos_weight * pos_loss + \
                     self.config.loss_config.rot_weight * rot_loss + \
                     open_loss + stop_loss
        
        return {
            'pos': pos_loss, 'rot': rot_loss, 'open': open_loss, 'stop': stop_loss,
            'total': total_loss
        }


class MotionPlannerPTV3CA(MotionPlannerPTV3AdaNorm):
    """Cross attention conditioned on text/pose/stepid
    """
    def __init__(self, config):
        BaseModel.__init__(self)

        config.defrost()
        config.ptv3_config.pdnorm_only_decoder = config.ptv3_config.get('pdnorm_only_decoder', False)
        config.ptv3_config.in_channels += config.action_config.pc_label_channels
        config.freeze()

        self.config = config

        self.ptv3_model = PointTransformerV3CA(**config.ptv3_config)

        # 0: obstacle, 1: robot, 2: object, 3: target
        self.pc_label_embedding = nn.Embedding(4, config.action_config.pc_label_channels)

        act_cfg = config.action_config
        self.txt_fc = nn.Linear(act_cfg.txt_ft_size, act_cfg.context_channels)
        if act_cfg.txt_reduce == 'attn':
            self.txt_attn_fc = nn.Linear(act_cfg.txt_ft_size, 1)
        if act_cfg.use_ee_pose:
            self.pose_embedding = RobotPoseEmbedding(act_cfg.context_channels)

        self.act_proj_head = ActionHead(
            act_cfg.reduce, act_cfg.pos_pred_type, act_cfg.rot_pred_type, 
            config.ptv3_config.dec_channels[0], act_cfg.dim_actions, act_cfg.max_traj_len,
            dropout=act_cfg.dropout, voxel_size=act_cfg.voxel_size,
            ptv3_config=config.ptv3_config, pos_bins=config.action_config.pos_bins,
            traj_embed_size=config.action_config.traj_embed_size
        )

        self.apply(self._init_weights)

        self.rot_transform = RotationMatrixTransform()

    def prepare_ptv3_batch(self, batch):
        outs = {
            'coord': batch['pc_fts'][:, :3],
            'grid_size': self.config.action_config.voxel_size,
            'offset': batch['offset'],
            'batch': offset2batch(batch['offset']),
            'feat': batch['pc_fts'],
        }
        pc_label_embeds = (self.pc_label_embedding(torch.LongTensor([0, 1, 2, 3]).to(device=batch['pc_labels'].device))[None,:,:]*batch['pc_labels'][:,:,None]).sum(dim=1)
        #pc_label_embeds = self.pc_label_embedding(batch['pc_labels'])
        outs['feat'] = torch.cat([outs['feat'], pc_label_embeds], dim=-1)

        device = batch['pc_fts'].device

        # encode context for each point cloud
        txt_embeds = self.txt_fc(batch['txt_embeds'])
        ctx_embeds = torch.split(txt_embeds, batch['txt_lens'])
        ctx_lens = torch.LongTensor(batch['txt_lens'])

        if self.config.action_config.use_ee_pose:
            pose_embeds = self.pose_embedding(batch['ee_poses'])
            ctx_embeds = [torch.cat([c, e.unsqueeze(0)], dim=0) for c, e in zip(ctx_embeds, pose_embeds)]
            ctx_lens += 1

        outs['context'] = torch.cat(ctx_embeds, 0)
        outs['context_offset'] = torch.cumsum(ctx_lens, dim=0).to(device)

        return outs


if __name__ == '__main__':
    from genrobo3d.configs.default import get_config

    config = get_config('genrobo3d/configs/rlbench/motion_planner_ptv3.yaml')
    model = MotionPlannerPTV3AdaNorm(config.MODEL).cuda()

    fake_batch = {
        'pc_fts': torch.rand(100, 6),
        'npoints_in_batch': [30, 70],
        'offset': torch.LongTensor([30, 100]),
        'txt_embeds': torch.rand(2, 512),
        'txt_lens': [1, 1],
        'ee_poses': torch.rand(2, 8),
        'gt_trajs': torch.rand(2, 5, 3+3+1),
    }

    outs = model(fake_batch, compute_loss=True)
    print(outs[1])
