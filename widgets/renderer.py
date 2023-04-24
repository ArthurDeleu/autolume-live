# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import sys
import copy
import traceback

import kornia
import numpy as np
import torch
import torch.fft
import torch.nn
import matplotlib.cm
import dnnlib
from bending.transform_layers import ManipulationLayer
from torch_utils.ops import upfirdn2d
import legacy  # pylint: disable=import-error
from net_base import SRVGGNetPlus

super_res = SRVGGNetPlus(num_in_ch=3, num_out_ch=3, num_feat=48, upscale=4, act_type='prelu').eval().to('cuda')
model_sd=torch.load('./sr_models/Fast.pt')
super_res.load_state_dict(model_sd)

# ----------------------------------------------------------------------------

def rgb2ycbcr(src):
    R = src[0]
    G = src[1]
    B = src[2]

    ycbcr = torch.zeros(size=src.shape)
    # *Intel IPP
    # ycbcr[0] = 0.257 * R + 0.504 * G + 0.098 * B + 16
    # ycbcr[1] = -0.148 * R - 0.291 * G + 0.439 * B + 128
    # ycbcr[2] = 0.439 * R - 0.368 * G - 0.071 * B + 128
    # *Intel IPP specific for the JPEG codec
    ycbcr[0] = 0.299 * R + 0.587 * G + 0.114 * B
    ycbcr[1] = -0.16874 * R - 0.33126 * G + 0.5 * B + 128
    ycbcr[2] = 0.5 * R - 0.41869 * G - 0.08131 * B + 128

    # Y in range [16, 235]
    ycbcr[0] = torch.clip(ycbcr[0], 16, 235)
    # Cb, Cr in range [16, 240]
    ycbcr[[1, 2]] = torch.clip(ycbcr[[1, 2]], 16, 240)
    ycbcr = ycbcr.type(torch.uint8)
    return ycbcr


def ycbcr2rgb(src):
    Y = src[0]
    Cb = src[1]
    Cr = src[2]

    rgb = torch.zeros(size=src.shape)
    # *Intel IPP
    # rgb[0] = 1.164 * (Y - 16) + 1.596 * (Cr - 128)
    # rgb[1] = 1.164 * (Y - 16) - 0.813 * (Cr - 128) - 0.392 * (Cb - 128)
    # rgb[2] = 1.164 * (Y - 16) + 2.017 * (Cb - 128)
    # *Intel IPP specific for the JPEG codec
    rgb[0] = Y + 1.402 * Cr - 179.456
    rgb[1] = Y - 0.34414 * Cb - 0.71414 * Cr + 135.45984
    rgb[2] = Y + 1.772 * Cb - 226.816

    rgb = torch.clip(rgb, 0, 255)
    rgb = rgb.type(torch.uint8)
    return rgb


# ----------------------------------------------------------------------------

class CapturedException(Exception):
    def __init__(self, msg=None):
        if msg is None:
            _type, value, _traceback = sys.exc_info()
            assert value is not None
            if isinstance(value, CapturedException):
                msg = str(value)
            else:
                msg = traceback.format_exc()
        assert isinstance(msg, str)
        super().__init__(msg)


# ----------------------------------------------------------------------------

class CaptureSuccess(Exception):
    def __init__(self, out):
        super().__init__()
        self.out = out


# ----------------------------------------------------------------------------

def _sinc(x):
    y = (x * np.pi).abs()
    z = torch.sin(y) / y.clamp(1e-30, float('inf'))
    return torch.where(y < 1e-30, torch.ones_like(x), z)


def _lanczos_window(x, a):
    x = x.abs() / a
    return torch.where(x < 1, _sinc(x), torch.zeros_like(x))


# ----------------------------------------------------------------------------

def _construct_affine_bandlimit_filter(mat, a=3, amax=16, aflt=64, up=4, cutoff_in=1, cutoff_out=1):
    assert a <= amax < aflt
    mat = torch.as_tensor(mat).to(torch.float32)

    # Construct 2D filter taps in input & output coordinate spaces.
    taps = ((torch.arange(aflt * up * 2 - 1, device=mat.device) + 1) / up - aflt).roll(1 - aflt * up)
    yi, xi = torch.meshgrid(taps, taps)
    xo, yo = (torch.stack([xi, yi], dim=2) @ mat[:2, :2].t()).unbind(2)

    # Convolution of two oriented 2D sinc filters.
    fi = _sinc(xi * cutoff_in) * _sinc(yi * cutoff_in)
    fo = _sinc(xo * cutoff_out) * _sinc(yo * cutoff_out)
    f = torch.fft.ifftn(torch.fft.fftn(fi) * torch.fft.fftn(fo)).real

    # Convolution of two oriented 2D Lanczos windows.
    wi = _lanczos_window(xi, a) * _lanczos_window(yi, a)
    wo = _lanczos_window(xo, a) * _lanczos_window(yo, a)
    w = torch.fft.ifftn(torch.fft.fftn(wi) * torch.fft.fftn(wo)).real

    # Construct windowed FIR filter.
    f = f * w

    # Finalize.
    c = (aflt - amax) * up
    f = f.roll([aflt * up - 1] * 2, dims=[0, 1])[c:-c, c:-c]
    f = torch.nn.functional.pad(f, [0, 1, 0, 1]).reshape(amax * 2, up, amax * 2, up)
    f = f / f.sum([0, 2], keepdim=True) / (up ** 2)
    f = f.reshape(amax * 2 * up, amax * 2 * up)[:-1, :-1]
    return f


# ----------------------------------------------------------------------------

def _apply_affine_transformation(x, mat, up=4, **filter_kwargs):
    _N, _C, H, W = x.shape
    mat = torch.as_tensor(mat).to(dtype=torch.float32, device=x.device)

    # Construct filter.
    f = _construct_affine_bandlimit_filter(mat, up=up, **filter_kwargs)
    assert f.ndim == 2 and f.shape[0] == f.shape[1] and f.shape[0] % 2 == 1
    p = f.shape[0] // 2

    # Construct sampling grid.
    theta = mat.inverse()
    theta[:2, 2] *= 2
    theta[0, 2] += 1 / up / W
    theta[1, 2] += 1 / up / H
    theta[0, :] *= W / (W + p / up * 2)
    theta[1, :] *= H / (H + p / up * 2)
    theta = theta[:2, :3].unsqueeze(0).repeat([x.shape[0], 1, 1])
    g = torch.nn.functional.affine_grid(theta, x.shape, align_corners=False)

    # Resample image.
    y = upfirdn2d.upsample2d(x=x, f=f, up=up, padding=p)
    z = torch.nn.functional.grid_sample(y, g, mode='bilinear', padding_mode='zeros', align_corners=False)

    # Form mask.
    m = torch.zeros_like(y)
    c = p * 2 + 1
    m[:, :, c:-c, c:-c] = 1
    m = torch.nn.functional.grid_sample(m, g, mode='nearest', padding_mode='zeros', align_corners=False)
    return z, m


# ----------------------------------------------------------------------------
def slerp(t, v0, v1, DOT_THRESHOLD=0.9995):
    '''
    Spherical linear interpolation
    Args:
        t (float/np.ndarray): Float value between 0.0 and 1.0
        v0 (np.ndarray): Starting vector
        v1 (np.ndarray): Final vector
        DOT_THRESHOLD (float): Threshold for considering the two vectors as
                               colineal. Not recommended to alter this.
    Returns:
        v2 (np.ndarray): Interpolation vector between v0 and v1
    '''
    v0 = v0.cpu().detach().numpy()
    v1 = v1.cpu().detach().numpy()
    # Copy the vectors to reuse them later
    v0_copy = np.copy(v0)
    v1_copy = np.copy(v1)
    # Normalize the vectors to get the directions and angles
    v0 = v0 / np.linalg.norm(v0)
    v1 = v1 / np.linalg.norm(v1)
    # Dot product with the normalized vectors (can't use np.dot in W)
    dot = np.sum(v0 * v1)
    # If absolute value of dot product is almost 1, vectors are ~colineal, so use lerp
    if np.abs(dot) > DOT_THRESHOLD:
        return torch.from_numpy(v0_copy + (v1_copy - v0_copy) * t)
    # Calculate initial angle between v0 and v1
    theta_0 = np.arccos(dot)
    sin_theta_0 = np.sin(theta_0)
    # Angle at timestep t
    theta_t = theta_0 * t
    sin_theta_t = np.sin(theta_t)
    # Finish the slerp algorithm
    s0 = np.sin(theta_0 - theta_t) / sin_theta_0
    s1 = sin_theta_t / sin_theta_0
    v2 = s0 * v0_copy + s1 * v1_copy
    return torch.from_numpy(v2)


class Renderer:
    def __init__(self):
        self._device = torch.device('cuda')
        self._pkl_data = dict()  # {pkl: dict | CapturedException, ...}
        self._networks = dict()  # {cache_key: torch.nn.Module, ...}
        self._pinned_bufs = dict()  # {(shape, dtype): torch.Tensor, ...}
        self._cmaps = dict()  # {name: torch.Tensor, ...}
        self._is_timing = False
        self._start_event = torch.cuda.Event(enable_timing=True)
        self._end_event = torch.cuda.Event(enable_timing=True)
        self._net_layers = dict()  # {cache_key: [dnnlib.EasyDict, ...], ...}
        self.manipulation = ManipulationLayer()

    def render(self, **args):
        self._is_timing = True
        self._start_event.record(torch.cuda.current_stream(self._device))
        res = dnnlib.EasyDict()
        try:
            self._render_impl(res, **args)
        except:
            res.error = CapturedException()
        self._end_event.record(torch.cuda.current_stream(self._device))
        if 'image' in res:
            res.image = self.to_cpu(res.image).numpy()
        if 'stats' in res:
            res.stats = self.to_cpu(res.stats).numpy()
        if 'error' in res:
            res.error = str(res.error)
        if self._is_timing:
            self._end_event.synchronize()
            res.render_time = self._start_event.elapsed_time(self._end_event) * 1e-3
            self._is_timing = False
        return res

    def get_network(self, pkl, key, **tweak_kwargs):
        data = self._pkl_data.get(pkl, None)
        if data is None:
            print(f'Loading "{pkl}"... ', end='', flush=True)
            try:
                with dnnlib.util.open_url(pkl, verbose=False) as f:
                    data = legacy.load_network_pkl(f, custom=True)
                print('Done.')
            except:
                data = CapturedException()
                print('Failed!')
            self._pkl_data[pkl] = data
            self._ignore_timing()
        if isinstance(data, CapturedException):
            raise data

        orig_net = data[key]
        cache_key = (orig_net, self._device, tuple(sorted(tweak_kwargs.items())))
        net = self._networks.get(cache_key, None)
        if net is None:
            try:
                net = copy.deepcopy(orig_net)
                net = net.to(self._device).eval().requires_grad_(False)
            except:
                net = CapturedException()
            self._networks[cache_key] = net
            self._ignore_timing()
        if isinstance(net, CapturedException):
            raise net
        return net



    def _get_pinned_buf(self, ref):
        key = (tuple(ref.shape), ref.dtype)
        buf = self._pinned_bufs.get(key, None)
        if buf is None:
            buf = torch.empty(ref.shape, dtype=ref.dtype).pin_memory()
            self._pinned_bufs[key] = buf
        return buf

    def to_device(self, buf):
        return buf.to(self._device) #self._get_pinned_buf(buf).copy_(buf,non_blocking=True).to(self._device)

    def to_cpu(self, buf):
        return buf.detach().cpu() #self._get_pinned_buf(buf).copy_(buf,non_blocking=True).clone()

    def _ignore_timing(self):
        self._is_timing = False

    def _apply_cmap(self, x, name='viridis'):
        cmap = self._cmaps.get(name, None)
        if cmap is None:
            cmap = matplotlib.cm.get_cmap(name)
            cmap = cmap(np.linspace(0, 1, num=1024), bytes=True)[:, :3]
            cmap = self.to_device(torch.from_numpy(cmap))
            self._cmaps[name] = cmap
        hi = cmap.shape[0] - 1
        x = (x * hi + 0.5).clamp(0, hi).to(torch.int64)
        x = torch.nn.functional.embedding(x, cmap)
        return x

    def evaluate(self, G, looping_index, looping_seeds, looping_projections, trunc_psi, trunc_cutoff):
        latent = looping_seeds[looping_index]
        w0_seeds = []
        for ofs_x, ofs_y in [[0, 0], [1, 0], [0, 1], [1, 1]]:
            seed_x = np.floor(latent.x) + ofs_x
            seed_y = np.floor(latent.y) + ofs_y
            seed = (int(seed_x) + int(seed_y) * 100) & ((1 << 32) - 1)
            weight = (1 - abs(latent.x - seed_x)) * (1 - abs(latent.y - seed_y))
            if weight > 0:
                w0_seeds.append([seed, weight])
        all_seeds = [seed for seed, _weight in w0_seeds]  # + [mappingmix_seed]
        all_seeds = list(set(all_seeds))
        all_zs = np.zeros([len(all_seeds), G.w_dim], dtype=np.float32)
        for idx, seed in enumerate(all_seeds):
            rnd = np.random.RandomState(seed)
            all_zs[idx] = rnd.randn(G.w_dim)

        all_cs = np.zeros([len(all_seeds), G.c_dim], dtype=np.float32)
        all_zs = np.zeros([len(all_seeds), G.z_dim], dtype=np.float32)
        for idx, seed in enumerate(all_seeds):
            rnd = np.random.RandomState(seed)
            all_zs[idx] = rnd.randn(G.z_dim)
            if G.c_dim > 0:
                all_cs[idx, rnd.randint(G.c_dim)] = 1

        if not looping_projections[looping_index]:
            all_ws = self.to_device(torch.from_numpy(all_zs))
            all_ws = dict(zip(all_seeds, all_ws))
            w = torch.stack([all_ws[seed] * weight for seed, weight in w0_seeds]).sum(dim=0, keepdim=True)
        # Run mapping network.
        else:
            w_avg = G.mapping.w_avg
            all_zs = torch.from_numpy(all_zs).to(self._device)#self.to_device(torch.from_numpy(all_zs))
            all_cs = torch.from_numpy(all_cs).to(self._device) #self.to_device(torch.from_numpy(all_cs))
            all_ws = G.mapping(z=all_zs, c=all_cs, truncation_psi=trunc_psi,
                               truncation_cutoff=trunc_cutoff) - w_avg
            all_ws = dict(zip(all_seeds, all_ws))
            w = torch.stack([all_ws[seed] * weight for seed, weight in w0_seeds]).sum(dim=0, keepdim=True)
            w += w_avg
        return w

    def _render_impl(self, res,
                     pkl=None,
                     w0_seeds=[[0, 1]],
                     vec=torch.randn(1, 512),
                     stylemix_idx=[],
                     stylemix_seed=0,
                     mode=True,
                     project=True,
                     trunc_psi=1,
                     trunc_cutoff=0,
                     random_seed=0,
                     noise_mode='const',
                     force_fp32=False,
                     layer_name=None,
                     sel_channels=3,
                     base_channel=0,
                     img_scale_db=0,
                     img_normalize=False,
                     fft_show=False,
                     fft_all=True,
                     fft_range_db=50,
                     fft_beta=8,
                     input_transform=None,
                     untransform=False,
                     latent_transforms=[],
                     adjustments={},
                     ratios={},
                     direction=torch.zeros(512),
                     noise_adjustments=None,
                     looping=False,
                     looping_index=0,
                     alpha=0,
                     looping_modes=None,
                     looping_seeds=None,
                     looping_keyframes=None,
                     looping_projections=None,
                     use_superres=False,
                     ):
        with torch.inference_mode():
            # Dig up network details.
            G = self.get_network(pkl, 'G_ema')
            res.img_resolution = G.img_resolution
            res.num_ws = G.num_ws
            res.has_noise = any('noise_const' in name for name, _buf in G.synthesis.named_buffers())
            res.has_input_transform = (hasattr(G.synthesis, 'input') and hasattr(G.synthesis.input, 'transform'))

            # Set input transform.
            if res.has_input_transform:
                m = np.eye(3)
                try:
                    if input_transform is not None:
                        m = np.linalg.inv(np.asarray(input_transform))
                except np.linalg.LinAlgError:
                    res.error = CapturedException()
                G.synthesis.input.transform.copy_(torch.from_numpy(m))

            # Generate random latents. Either project with intermediate mapping model or not
            if not looping:
                if mode:
                    all_seeds = [seed for seed, _weight in w0_seeds]  # + [stylemix_seed]
                    all_seeds = list(set(all_seeds))
                    all_cs = np.zeros([len(all_seeds), G.c_dim], dtype=np.float32)
                    all_zs = np.zeros([len(all_seeds), G.z_dim], dtype=np.float32)
                    for idx, seed in enumerate(all_seeds):
                        rnd = np.random.RandomState(seed)
                        all_zs[idx] = rnd.randn(G.z_dim)
                        if G.c_dim > 0:
                            all_cs[idx, rnd.randint(G.c_dim)] = 1

                    if not project:
                        all_ws = self.to_device(torch.from_numpy(all_zs))
                        all_ws = dict(zip(all_seeds, all_ws))
                        w = torch.stack([all_ws[seed] * weight for seed, weight in w0_seeds]).sum(dim=0, keepdim=True)
                    # Run mapping network.
                    else:
                        w_avg = G.mapping.w_avg
                        all_zs = self.to_device(torch.from_numpy(all_zs))
                        all_cs = self.to_device(torch.from_numpy(all_cs))
                        all_ws = G.mapping(z=all_zs, c=all_cs, truncation_psi=trunc_psi,
                                           truncation_cutoff=trunc_cutoff) - w_avg
                        all_ws = dict(zip(all_seeds, all_ws))
                        w = torch.stack([all_ws[seed] * weight for seed, weight in w0_seeds]).sum(dim=0, keepdim=True)
                        w += w_avg

                else:
                    try:
                        all_cs = np.zeros([len(vec), G.c_dim], dtype=np.float32)
                        w = self.to_device(vec)
                        if project:
                            w = G.mapping(z=w, c=all_cs, truncation_psi=trunc_psi, truncation_cutoff=trunc_cutoff)
                    except Exception as e:
                        print(e)

                if not project:
                    w = w.repeat(G.num_ws, 1).unsqueeze(0)
            else:
                if looping_modes[looping_index - 1]:
                    v0 = self.evaluate(G, looping_index - 1, looping_seeds, looping_projections, trunc_psi,
                                       trunc_cutoff)
                else:
                    v0 = self.to_device(looping_keyframes[looping_index - 1][None])
                    all_cs = np.zeros([len(v0), G.c_dim], dtype=np.float32)
                    if looping_projections[looping_index - 1]:
                        v0 = G.mapping(v0, all_cs)

                if looping_modes[looping_index]:
                    v1 = self.evaluate(G, looping_index, looping_seeds, looping_projections, trunc_psi, trunc_cutoff)
                else:
                    v1 = self.to_device(looping_keyframes[looping_index][None])
                    all_cs = np.zeros([len(v1), G.c_dim], dtype=np.float32)
                    if looping_projections[looping_index]:
                        v1 = G.mapping(v1, all_cs)
                if len(v0.shape) == 2:
                    v0 = v0.repeat(G.num_ws, 1).unsqueeze(0)
                if len(v1.shape) == 2:
                    v1 = v1.repeat(G.num_ws, 1).unsqueeze(0)
                w = self.to_device(slerp(alpha, v0, v1))

            # Run synthesis network.
            synthesis_kwargs = dnnlib.EasyDict(noise_mode=noise_mode, force_fp32=force_fp32)
            torch.manual_seed(random_seed)
            w += self.to_device(direction)
            out, manip_layers = self.run_synthesis_net(G.synthesis, w, capture_layer=layer_name, transforms=latent_transforms,
                                                 adjustments=adjustments, noise_adjustments=noise_adjustments, ratios=ratios, use_superres=use_superres,
                                                 **synthesis_kwargs)
            res.snap = self.to_cpu(w).squeeze()[0]

            # Update layer list.
            cache_key = (G.synthesis, tuple(sorted(synthesis_kwargs.items())))
            if cache_key not in self._net_layers:
                print("Generating layer list for...")
                layers = manip_layers
                if layer_name is not None:
                    torch.manual_seed(random_seed)
                    _out, layers = self.run_synthesis_net(G.synthesis, w, use_superres=False,**synthesis_kwargs)
                self._net_layers[cache_key] = layers
                del layers

            res.layers = self._net_layers[cache_key]
            res.layers[:len(manip_layers)] = manip_layers

            # Untransform.
            if untransform and res.has_input_transform:
                out, _mask = _apply_affine_transformation(out.to(torch.float32), G.synthesis.input.transform,
                                                          amax=6)  # Override amax to hit the fast path in upfirdn2d.

            # Select channels and compute statistics.
            out = out[0].to(torch.float32)
            if sel_channels > out.shape[0]:
                sel_channels = 1
            base_channel = max(min(base_channel, out.shape[0] - sel_channels), 0)
            sel = out[base_channel: base_channel + sel_channels]
            res.stats = torch.stack([
                out.mean(), sel.mean(),
                out.std(), sel.std(),
                out.norm(float('inf')), sel.norm(float('inf')),
            ])

            # Scale and convert to uint8.
            img = sel
            if img_normalize:
                img = img / img.norm(float('inf'), dim=[1, 2], keepdim=True).clip(1e-8, 1e8)
            img = img * (10 ** (img_scale_db / 20))
            img = (img * 127.5 + 128).clamp(0, 255).to(torch.uint8).permute(1, 2, 0)
            res.image = img

            del img
            del manip_layers
            del out # Free up GPU memory.

    def run_synthesis_net(self, net, *args, capture_layer=None, transforms=None, ratios=None, adjustments=None,
                          noise_adjustments=None, use_superres=False, **kwargs):
        """
        Run the synthesis network and capture the output of a specific layer.
        :param net: Synthesis model
        :param args: synthesis model inputs
        :param capture_layer: which layer to capture
        :param transforms: list(dict(layer name: dict(TransformID, LayerID, params))), latent transforms
        :param ratios: dict(layer name: tuple(width, height)), ratios for each layer
        :param adjustments: dict(layer name: vector) Vector that is added per layer to the latent vector
        :param noise_adjustments: noise adjustments
        :param kwargs: further synthesis kwargs
        :return: img and dict of layers
        """
        if noise_adjustments is None:
            noise_adjustments = {}
        if adjustments is None:
            adjustments = {}
        if ratios is None:
            ratios = {}
        if transforms is None:
            transforms = []
        submodule_names = {mod: name for name, mod in net.named_modules()}
        unique_names = set()
        layers = []
        def adjustment_hook(module, inputs):
            #pre forward hook to add adjustments to the latent vector and resize input to fit ratio
            inps = []
            for inp in inputs:
                if inp is not None:
                    inps.append(inp.shape)
                else:
                    inps.append(None)
            name = submodule_names[module]
            if "conv" in name and not ('affine' in name):
                # if not affine means has noise parameter and is convolutional
                if name in noise_adjustments:
                    noise_strength = noise_adjustments[name]["strength"]
                    module.noise_regulator = noise_strength
                else:
                    module.noise_regulator = 0

                if name in ratios:
                    # if layer is in ratios, resize activations to fit ratio
                    rx, ry = ratios[name]
                    module.ratio = (rx, ry)

            if "affine" in name and name in adjustments.keys():
                # if affine (style vector insertion) and in adjustments, add adjustments to latent vector
                adj = adjustments[name]
                return inputs[0] + adj.to(device=inputs[0].device, dtype=inputs[0].dtype)

        def module_hook(module, _inputs, outputs):
            #post forward hook to capture output of specific layer and apply transformations (network bending) on output
            outputs = list(outputs) if isinstance(outputs, (tuple, list)) else [outputs]
            outputs = [out for out in outputs if isinstance(out, torch.Tensor) and out.ndim in [4, 5]]
            name = submodule_names[module]
            if name == '':
                name = 'output'
            if len(outputs) == 1:
                for transform in transforms:
                    if name == transform["layerID"]:
                        try:
                            outputs = [self.manipulation(outputs[0], transform)]
                        except Exception as e:
                            print(e)

            for idx, out in enumerate(outputs):
                if out.ndim == 5:  # G-CNN => remove group dimension.
                    out = out.mean(2)
                name = submodule_names[module]
                if name == '':
                    name = 'output'
                if len(outputs) > 1:
                    name += f':{idx}'
                if name in unique_names:
                    suffix = 2
                    while f'{name}_{suffix}' in unique_names:
                        suffix += 1
                    name += f'_{suffix}'
                unique_names.add(name)
                shape = [int(x) for x in out.shape]
                dtype = str(out.dtype).split('.')[-1]
                layers.append(dnnlib.EasyDict(name=name, shape=shape, dtype=dtype))
                if name == capture_layer:
                    raise CaptureSuccess(out)

        hooks = [module.register_forward_hook(module_hook) for module in net.modules()]
        hooks.extend([module.register_forward_pre_hook(adjustment_hook) for module in net.modules()])
        try:
            with torch.no_grad():
                out, _ = net(*args, **kwargs)
                if isinstance(out, tuple):
                    out = out[0]
                if use_superres:
                    with torch.autocast("cuda"):
                        out = super_res(out)
                print("out, ", out.shape)
        except CaptureSuccess as e:
            out = e.out
        for hook in hooks:
            hook.remove()
        return out, layers

# ----------------------------------------------------------------------------
