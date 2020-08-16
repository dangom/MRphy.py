import numpy as np
import torch
from torch import tensor, cuda, Tensor
from typing import TypeVar, Type, Union
from numbers import Number

from mrphy import γH, dt0, T1G, T2G, π
from mrphy import utils, beffective, sims

"""
"""

# TODO:
# - Abstract Class
# - Non-compact SpinCube initialization


Pulse = TypeVar('Pulse', bound='Pulse')
SpinArray = TypeVar('SpinArray', bound='SpinArray')
SpinCube = TypeVar('SpinCube', bound='SpinCube')


class Pulse(object):
    """
    # Attributes:
    - `rf` (N,xy, nT,(nCoils)) "Gauss", `xy` for separating real and imag part.
    - `gr` (N,xyz,nT) "Gauss/cm"
    - `dt` (N,1,), "Sec" simulation temporal step size, i.e., dwell time.
    - `desc` str, an description of the pulse to be constructed.
    """

    _readonly = ('device', 'dtype')
    __slots__ = set(_readonly + ('rf', 'gr', 'dt', 'desc'))

    def __init__(
            self,
            rf: Tensor = None, gr: Tensor = None, dt: Tensor = dt0,
            desc: str = "generic pulse",
            device: torch.device = torch.device('cpu'),
            dtype: torch.dtype = torch.float32):

        assert(isinstance(device, torch.device) and
               isinstance(dtype, torch.dtype))

        # Defaults
        rf_miss, gr_miss = rf is None, gr is None
        assert (not(rf_miss and gr_miss)), "Missing both `rf` and `gr` inputs"

        super().__setattr__('device', device)
        super().__setattr__('dtype', dtype)

        kw = {'device': self.device, 'dtype': self.dtype}

        if rf_miss:
            N, nT = gr.shape[0], gr.shape[2]
            rf = torch.zeros((N, 2, nT), **kw)

        if gr_miss:
            N, nT = rf.shape[0], rf.shape[2]
            gr = torch.zeros((N, 3, nT), **kw)

        # super() here, as self.__setattr__() has interdependent sanity check.
        rf, gr = rf.to(**kw), gr.to(**kw)
        assert (rf.shape[0] == gr.shape[0] and rf.shape[2] == gr.shape[2])

        super().__setattr__('rf', rf)
        super().__setattr__('gr', gr)

        self.dt, self.desc = dt, desc
        return

    def __setattr__(self, k, v):
        assert (k not in self._readonly), "'%s' is read-only." % k

        if k != 'desc':
            kw = {'device': self.device, 'dtype': self.dtype}
            v = (v.to(**kw) if isinstance(v, Tensor) else tensor(v, **kw))

        if (k == 'gr'):
            shape = self.rf.shape
            assert (v.shape[0] == shape[0] and v.shape[2] == shape[2])
        if (k == 'rf'):
            shape = self.gr.shape
            assert (v.shape[0] == shape[0] and v.shape[2] == shape[2])

        super().__setattr__(k, v)
        return

    def asdict(self, toNumpy: bool = True) -> dict:
        _ = ('rf', 'gr', 'dt')
        fn_np = (lambda x: x.detach().cpu().numpy() if toNumpy else
                 lambda x: x.detach())

        d = {k: fn_np(getattr(self, k)) for k in _}
        d.update({k: getattr(self, k) for k in ('desc', 'device', 'dtype')})

        return d

    def beff(
            self, loc: Tensor,
            Δf: Tensor = None, b1Map: Tensor = None, γ: Tensor = γH) -> Tensor:
        """
        *INPUTS*:
        - `loc`   (N,*Nd,xyz) "cm", locations.
        *OPTIONALS*:
        - `Δf`    (N,*Nd,) "Hz", off-resonance.
        - `b1Map` (N,*Nd,xy,(nCoils)) a.u., , transmit sensitivity.
        - `γ`     (N,*Nd) "Hz/Gauss", gyro-ratio
        *OUTPUTS*:
        - `beff`  (N,*Nd,xyz,nT)
        """
        device = self.device
        loc = loc.to(device=device)
        fn = lambda x: None if x is None else x.to(device=device)  # noqa: E731
        Δf, b1Map, γ = (fn(x) for x in (Δf, b1Map, γ))

        return beffective.rfgr2beff(self.rf, self.gr, loc,
                                    Δf=Δf, b1Map=b1Map, γ=γ)

    def to(self, device: torch.device = torch.device('cpu'),
           dtype: torch.dtype = torch.float32) -> Pulse:
        if (self.device != device) or (self.dtype != dtype):
            return Pulse(self.rf, self.gr, dt=self.dt, desc=self.desc,
                         device=device, dtype=dtype)
        else:
            return self
        return


class SpinArray(object):
    """
        SpinArray(shape; T1_, T2_, γ_, M_, device, dtype)
    *INPUTS*:
    - `shape` tuple( (N, nx, (ny, (nz,...))) ).
    *OPTIONALS*:
    - `mask` (1, *Nd) Tensor, where does compact attributes locate in `Nd`.
    - `T1_` (N, nM) Tensor "Sec", T1 relaxation coeff.
    - `T2_` (N, nM) Tensor "Sec", T2 relaxation coeff.
    - `γ_`  (N, nM) Tensor "Hz/Gauss", gyro ratio.
    - `M_`  (N, nM, xyz) Tensor, spins, assumed equilibrium [0 0 1]
    - `device` torch.device; `dtype` torch.dtype

    *PROPERTIES*:
    - `shape` (N, *Nd)
    - `mask`  (1, *Nd)
    - `device`
    - `dtype`
    - `ndim` (1,), `len(shape)`
    - `nM` (1,), `nM = mask.sum().item()`;
    - `T1_` (N, nM) Tensor "Sec", T1 relaxation coeff.
    - `T2_` (N, nM) Tensor "Sec", T2 relaxation coeff.
    - `γ_`  (N, nM) Tensor "Hz/Gauss", gyro ratio.
    - `M_`  (N, nM, xyz) Tensor, spins, assumed equilibrium [0 0 1]

    *WARNING*
    - Do NOT modify the `mask` of an object, e.g., `spinarray.mask[0] = True`.
    - Do NOT proceed indexed/masked assignments over any non-compact attribute,
    e.g., `spinarray.T1[0] = T1G` or `spinarray.T1[mask] = T1G`. The underlying
    compact attributes will NOT be updated, since they do not share memory. The
    only exception is when `torch.all(mask == True)` and the underlying compact
    is *contiguous*, where the non-compact is just a `view((N, *Nd, ...))`.
    Checkout `.crds_()` & `.mask_()` for indexed/masked access to compacts.

    *NOTE*
    - `mask` is GLOBAL for a batch, in other words, one cannot specify distinct
    masks w/in a batch. This design is to reduce storage/computations in, e.g.,
    `applypulse` (`blochsim`), avoiding extra allocations.
    For DNN applications where an in-batch variation of `mask` may seemingly be
    of interest, having `torch.all(mask == True)` and postponing the variations
    to eventual losses evaluation can be a better design, which allows reuse of
    `M_`, etc., avoiding repetitive allocations.
    """

    _readonly = ('shape', 'mask', 'device', 'dtype', 'ndim', 'nM')
    _compact = ('T1_', 'T2_', 'γ_', 'M_')
    __slots__ = set(_readonly + _compact)

    def __init__(
            self, shape: tuple, mask: Tensor = None,
            T1_: Tensor = T1G, T2_: Tensor = T2G,
            γ_: Tensor = γH, M_: Tensor = tensor([0., 0., 1.]),
            device: torch.device = torch.device('cpu'),
            dtype: torch.dtype = torch.float32):

        mask = (torch.ones((1,)+shape[1:], dtype=torch.bool, device=device)
                if mask is None else mask.to(device=device))

        assert(isinstance(device, torch.device) and
               isinstance(dtype, torch.dtype) and
               mask.dtype == torch.bool and
               mask.shape == (1,)+shape[1:])

        super().__setattr__('shape', shape)
        super().__setattr__('mask', mask)
        super().__setattr__('ndim', len(shape))
        super().__setattr__('nM', torch.sum(mask).item())
        super().__setattr__('device', device)
        super().__setattr__('dtype', dtype)

        self.T1_, self.T2_, self.γ_, self.M_ = T1_, T2_, γ_, M_
        return

    def __getattr__(self, k):
        if k+'_' not in self._compact:
            raise AttributeError("'SpinArray' has no attribute '%s'" % k)

        v_ = getattr(self, k+'_')
        return (self.embed(v_) if self.nM != np.prod(self.shape[1:]) else
                v_.reshape(self.shape+v_.shape[2:]))  # `mask` is all True

    def __setattr__(self, k_, v_):
        assert (k_ not in self._readonly), "'%s' is read-only." % k_

        # Transfer `v_` to `kw` before `extract`
        kw = {'device': self.device, 'dtype': self.dtype}
        v_ = (v_.to(**kw) if isinstance(v_, Tensor) else tensor(v_, **kw))

        if k_+'_' in self._compact:  # enable non-compact assignment
            assert (v_.shape[:self.ndim] == self.shape)
            k_, v_ = k_+'_', self.extract(v_)
            assert (k_ not in self._readonly), "'%s' is read-only." % k_

        # `tensor.expand(size)` needs `tensor.shape` broadcastable with `size`
        if k_ == 'M_':
            if v_.shape != self.shape[:1]+(self.nM, 3):  # (N, nM, xyz)
                v_ = v_.expand(self.shape[:1]+(self.nM, 3)).clone()
        elif k_ in self._compact:  # (T1_, T2_, γ_)
            v_ = v_.expand((self.shape[0], self.nM))  # (N, nM)

        super().__setattr__(k_, v_)
        return

    def applypulse(
            self, pulse: Pulse, doEmbed: bool = False,
            loc: Tensor = None, loc_: Tensor = None,
            Δf: Tensor = None, Δf_: Tensor = None,
            b1Map: Tensor = None, b1Map_: Tensor = None) -> Tensor:
        """
        *INPUTS*:
        - `pulse`
        - `loc` ^ `loc_`     (N,*Nd ^ nM,xyz) "cm", locations.
        *OPTIONALS*:
        - `doEmbed` [t/F]    return `M` or `M_`
        - `Δf`    | `Δf_`    (N,*Nd|nM) "Hz", off-resonance.
        - `b1Map` | `b1Map_` (N,*Nd|nM,xy,(nCoils)), transmit sensitivity.
        *OUTPUTS*:
        - `beff`  | `beff_`  (N,*Nd|nM,xyz,nT)
        """
        assert ((loc_ is None) != (loc is None))  # XOR
        loc_ = (loc_ if loc is None else self.extract(loc))

        assert ((Δf_ is None) or (Δf is None))
        Δf_ = (Δf_ if Δf is None else self.extract(Δf))

        assert ((b1Map_ is None) or (b1Map is None))
        b1Map_ = (b1Map_ if b1Map is None else self.extract(b1Map))

        beff_ = self.pulse2beff(pulse, loc_=loc_,
                                Δf_=Δf_, b1Map_=b1Map_, doEmbed=False)

        kw_bsim = {k[:-1]: getattr(self, k) for k in ('T1_', 'T2_', 'γ_')}
        kw_bsim['dt'] = pulse.dt

        M_ = sims.blochsim(self.M_, beff_, **kw_bsim)
        M_ = (self.embed(M_) if doEmbed else M_)
        return M_

    def asdict(self, toNumpy: bool = True, doEmbed: bool = True) -> dict:
        fn_np = (lambda x: x.detach().cpu().numpy() if toNumpy else
                 lambda x: x.detach())

        _ = (('T1', 'T2', 'γ', 'M') if doEmbed else ('T1_', 'T2_', 'γ_', 'M_'))
        d = {k: fn_np(getattr(self, k)) for k in _}
        d['mask'] = fn_np(getattr(self, 'mask'))

        d.update({k: getattr(self, k) for k in ('shape', 'device', 'dtype')})
        return d

    def crds_(self, crds: list) -> list:
        """
        Compute crds for compact attributes
        *INPUTS*:
        `crds`, capable indexing non-compact attributes.
        *OUTPUTS*:
        - `crds_` list, `len(crds_) == 2+len(crds)-self.ndim`.
        `v_[crds_] == v[crds]`, while `v_[crds_]=new_value` is effective.
        """
        mask, ndim, nM = self.mask, self.ndim, self.nM
        assert (len(crds) >= ndim)
        crds_ = [crds[i] for i in (0,)+tuple(range(ndim, len(crds)))]
        m = torch.zeros(mask.shape, dtype=tensor(mask.numel()).dtype)-1
        m[mask] = torch.arange(nM)
        inds_ = [ind_ for ind_ in m[[[0]]+crds[1:ndim]].tolist() if ind_ != -1]

        crds_.insert(1, inds_)

        return crds_

    def dim(self) -> int: return len(self.shape)

    def embed(self, v_: Tensor, out: Tensor = None) -> Tensor:
        """
        *INPUTS*
        - `v_` (N, nM, ...), must be contiguous
        *OPTIONALS*
        - `out` (N, *Nd, ...)
        *OUTPUTS*
        - `out` (N, *Nd, ...)
        """
        oshape = self.shape+v_.shape[2:]
        out = (v_.new_full(oshape, float('NaN')) if out is None else out)
        mask = self.mask.expand(self.shape)
        out[mask] = v_.view((-1,)+v_.shape[2:])
        # `v.reshape()` has intermediate alloc, leaving `out` pointless.
        # out[mask] = v_.reshape((-1,)+v_.shape[2:])
        return out

    def extract(self, v: Tensor, out_: Tensor = None) -> Tensor:
        """
        *INPUTS*
        - `v` (N, *Nd, ...)
        *OPTIONALS*
        - `out_` (N, nM, ...), must be contiguous.
        *OUTPUTS*
        - `out_` (N, nM, ...)
        """
        oshape = (self.shape[0], self.nM)+v.shape[self.ndim:]
        out_ = (v.new_empty(oshape) if out_ is None else out_)
        mask = self.mask.expand(self.shape)
        # ! do NOT use `out_.reshape()`; It creats new tensor when should fail.
        out_.view((-1,)+v.shape[self.ndim:]).copy_(v[mask])
        # `v[mask].reshape()` has intermediate alloc, leaving `out_` pointless.
        # out_.copy_(v[mask].reshape((-1,)+v.shape[self.ndim:]))
        return out_

    def mask_(self, mask: Tensor) -> Tensor:
        """
        *INPUTS*:
        - `mask` (1, *Nd).
        *OUTPTS*:
        - `mask_` (1, nM), `mask_` can be used on compact attributes
        """
        mask_ = mask(self.mask).reshape((1, -1))
        return mask_

    def numel(self) -> int: return self.mask.numel()

    def pulse2beff(
            self, pulse: Pulse, doEmbed: bool = False,
            loc: Tensor = None, loc_: Tensor = None,
            Δf: Tensor = None, Δf_: Tensor = None,
            b1Map: Tensor = None, b1Map_: Tensor = None) -> Tensor:
        """
        *INPUTS*:
        - `pulse`
        - `loc` ^ `loc_`     (N,*Nd ^ nM,xyz) "cm", locations.
        *OPTIONALS*:
        - `doEmbed` [t/F]  return `beff` or `beff_`
        - `Δf`    | `Δf_`    (N,*Nd|nM) "Hz", off-resonance.
        - `b1Map` | `b1Map_` (N,*Nd|nM,xy,(nCoils)), transmit sensitivity.
        *OUTPUTS*:
        - `beff`  | `beff_`  (N,*Nd|nM,xyz,nT)
        """
        assert ((loc_ is None) != (loc is None))  # XOR
        loc_ = (loc_ if loc is None else self.extract(loc))

        assert ((Δf_ is None) or (Δf is None))
        Δf_ = (Δf_ if Δf is None else self.extract(Δf))

        assert ((b1Map_ is None) or (b1Map is None))
        b1Map_ = (b1Map_ if b1Map is None else self.extract(b1Map))

        pulse = pulse.to(device=self.device, dtype=self.dtype)
        beff_ = pulse.beff(loc_, γ=self.γ_, Δf=Δf_, b1Map=b1Map_)
        beff_ = (self.embed(beff_) if doEmbed else beff_)
        return beff_

    def size(self) -> tuple: return self.shape

    def to(self, device: torch.device = torch.device('cpu'),
           dtype: torch.dtype = torch.float32) -> SpinArray:
        if self.device == device and self.dtype == dtype:
            return self
        return SpinArray(self.shape, self.mask, T1_=self.T1_, T2_=self.T2_,
                         γ_=self.γ_, M_=self.M_, device=device, dtype=dtype)


class SpinCube(object):
    """
        SpinCube(shape, fov; mask, ofst, Δf_, T1_, T2_, γ_, M_, device, dtype)
    *INPUTS*:
    - `shape` Tuple `(N, nx, (ny, (nz,...)))`.
    - `fov` (N, xyz) Tensor "cm", field of view.
    *OPTIONALS*:
    - `mask` (1, *Nd) Tensor, where does compact attributes locate in `Nd`.
    - `ofst` (N, xyz) Tensor "cm", fov offset from iso-center.
    - `Δf_` (N, nM) Tensor "Hz", off-resonance map.
    - `T1_` (N, nM) Tensor "Sec", T1 relaxation coeff.
    - `T2_` (N, nM) Tensor "Sec", T2 relaxation coeff.
    - `γ_`  (N, nM) Tensor "Hz/Gauss", gyro ratio.
    - `M_`  (N, nM, xyz) Tensor, spins, assumed equilibrium [0 0 1]
    - `device` torch.device; `dtype` torch.dtype

    *PROPERTIES*:
    - `spinarray` (1,) SpinArray object.
    - `Δf_` (N, nM) Tensor "Hz", off-resonance map.
    - `loc_` (N, nM, xyz) Tensor "cm", location of spins.
    - `fov` (N, xyz) Tensor "cm", field of view.
    - `ofst` (N, xyz) Tensor "cm", fov offset from iso-center.
    """

    _readonly = ('spinarray', 'loc_')
    _compact = ('Δf_', 'loc_')  # `loc_` depends on `shape`, `fov` and `ofst`
    __slots__ = set(_readonly+_compact+('fov', 'ofst'))

    def __init__(
            self, shape: tuple, fov: Tensor, mask: Tensor = None,
            ofst: Tensor = tensor([[0., 0., 0.]]), Δf_: Tensor = tensor(0.),
            T1_: Tensor = T1G, T2_: Tensor = T2G,
            γ_: Tensor = γH, M_: Tensor = tensor([0., 0., 1.]),
            device: torch.device = torch.device('cpu'),
            dtype: torch.dtype = torch.float32):
        """
        """
        super().__setattr__('spinarray',
                            SpinArray(shape, mask, T1_=T1_, T2_=T2_, γ_=γ_,
                                      M_=M_, device=device, dtype=dtype))
        sp = self.spinarray

        kw = {'device': sp.device, 'dtype': sp.dtype}
        # setattr(self, k, v), avoid computing `loc_` w/ `fov` & `ofst` not set
        super().__setattr__('fov', fov.to(**kw))
        super().__setattr__('ofst', ofst.to(**kw))
        # Initialize `loc_` in memory, reuse it.
        super().__setattr__('loc_', torch.zeros((sp.shape[0], sp.nM, 3), **kw))
        self._update_loc_()  # compute `loc_` from set `fov` & `ofst`

        self.Δf_ = Δf_
        return

    def __getattr__(self, k):  # provoked only when `__getattribute__` failed
        if k+'_' not in self._compact:  # k not in ('Δf_', 'loc')
            try:
                return getattr(self.spinarray, k)
            except AttributeError:
                raise AttributeError("'SpinCube' has no attribute '%s'" % k)

        v_, sp = getattr(self, k+'_'), self.spinarray
        return (sp.embed(v_) if sp.nM != np.prod(sp.shape[1:]) else
                v_.reshape(sp.shape+v_.shape[2:]))  # `mask` is all True

    def __setattr__(self, k_, v_):
        assert (k_ not in self._readonly), "'%s' is read-only." % k_

        sp = self.spinarray
        if k_ in SpinArray.__slots__ or k_+'_' in SpinArray.__slots__:
            setattr(sp, k_, v_)
            return

        kw = {'device': sp.device, 'dtype': sp.dtype}
        v_ = (v_.to(**kw) if isinstance(v_, Tensor) else tensor(v_, **kw))

        if k_+'_' in self._compact:  # `loc_` excluded by beginning assert
            assert (v_.shape[:sp.ndim] == sp.shape)
            k_, v_ = k_+'_', sp.extract(v_)
            assert (k_ not in self._readonly), "'%s' is read-only." % k_

        if k_ == 'Δf_':
            v_ = v_.expand((sp.shape[0], sp.nM))  # (N, nM)
        elif k_ in ('fov', 'ofst'):
            assert(v_.ndim == 2)

        super().__setattr__(k_, v_)

        # update `loc_` when needed
        if k_ in ('fov', 'ofst'):
            self._update_loc_()
        return

    def _update_loc_(self):
        loc_, fov, ofst = self.loc_, self.fov, self.ofst
        sp = self.spinarray
        kw = {'device': sp.device, 'dtype': sp.dtype}

        # locn (1, prod(Nd), xyz)  normalized locations, [-0.5, 0.5)
        shape, mask = sp.shape, sp.mask
        crdn = ((torch.arange(x, **kw)-utils.ctrsub(x))/x for x in shape[1:])
        _locn = torch.meshgrid(*crdn)  # ((*Nd,), (*Nd), (*Nd))

        for i in range(3):  # xyz, (N, nM)
            # According to `memory_profiler`, this does not provoke allocs.
            # `torch.addr`'s `vec2`, _locn[i][mask[0, ...]], provokes alloc.
            loc_[..., i] = (fov[:, None, i]*_locn[i][mask[0, ...]][None, ...]
                            + ofst[:, None, i])

        return

    def applypulse(
            self, pulse: Pulse, doEmbed: bool = False,
            b1Map: Tensor = None, b1Map_: Tensor = None) -> Tensor:
        """
        *INPUTS*:
        - `pulse`   (1,) mobjs.Pulse object
        *OPTIONALS*
        - `doEmbed` [t/F]    return `M` or `M_`
        - `b1Map` | `b1Map_` (N,*Nd|nM,xy,(nCoils)), transmit sensitivity.
        *OUTPUTS*:
        - `beff`  | `beff_`  (N,*Nd|nM,xyz,nT)
        """
        sp = self.spinarray
        assert ((b1Map_ is None) or (b1Map is None))
        b1Map_ = (b1Map_ if b1Map is None else self.extract(b1Map))

        return self.spinarray.applypulse(pulse, doEmbed=doEmbed, Δf_=self.Δf_,
                                          loc_=self.loc_, b1Map_=b1Map_)

    def asdict(self, toNumpy: bool = True, doEmbed: bool = True) -> dict:
        fn_np = (lambda x: x.detach().cpu().numpy() if toNumpy else
                 lambda x: x.detach())

        _ = (('loc', 'Δf') if doEmbed else ('loc', 'Δf'))
        d = {k: fn_np(getattr(self, k)) for k in _}

        d.update({k: getattr(self, k) for k in ('fov', 'ofst')})

        d.update(self.spinarray.asdict(toNumpy=toNumpy, doEmbed=doEmbed))
        return d

    def dim(self) -> int: return self.spinarray.dim()

    def crds_(self, crds: list) -> list: return self.spinarray.crds_(crds)

    def embed(self, v_: Tensor, out: Tensor = None) -> Tensor:
        """
        *INPUTS*
        - `v_` (N, nM, ...), must be contiguous
        *OPTIONALS*
        - `out` (N, *Nd, ...)
        *OUTPUTS*
        - `out` (N, *Nd, ...)
        """
        return self.spinarray.embed(v_, out=out)

    def extract(self, v: Tensor, out_: Tensor = None) -> Tensor:
        """
        *INPUTS*
        - `v` (N, *Nd, ...)
        *OPTIONALS*
        - `out_` (N, nM, ...), must be contiguous.
        *OUTPUTS*
        - `out_` (N, nM, ...)
        """
        return self.spinarray.extract(v, out_=out_)

    def mask_(self, mask: Tensor) -> Tensor: return self.spinarray.mask_(mask)

    def numel(self) -> int: return self.spinarray.numel()

    def pulse2beff(
            self, pulse: Pulse, doEmbed: bool = False,
            Δf: Tensor = None, Δf_: Tensor = None,
            b1Map: Tensor = None, b1Map_: Tensor = None) -> Tensor:
        """
        *INPUTS*:
        - `pulse`
        *OPTIONALS*:
        - `doEmbed` [t/F]  return `beff` or `beff_`
        - `Δf`    | `Δf_`    (N,*Nd|nM) "Hz", off-resonance.
        - `b1Map` | `b1Map_` (N,*Nd|nM,xy,(nCoils)), transmit sensitivity.
        *OUTPUTS*:
        - `beff`  | `beff_`  (N,*Nd|nM,xyz,nT)
        """
        return self.spinarray.pulse2beff(pulse, self.loc_, doEmbed=doEmbed,
                                         Δf=self.Δf,  Δf_=self.Δf_,
                                         b1Map=b1Map, b1Map_=b1Map_)

    def size(self) -> tuple: return self.spinarray.size()

    def to(self, device: torch.device = torch.device('cpu'),
           dtype: torch.dtype = torch.float32) -> SpinCube:
        if (self.device != device) or (self.dtype != dtype):
            return SpinCube(self.shape, self.fov, ofst=self.ofst, Δf_=self.Δf_,
                            T1_=self.T1_, T2_=self.T2_, γ_=self.γ_, M_=self.M_,
                            device=device, dtype=dtype)
        else:
            return self
        return


class SpinBolus(SpinArray):
    def __init__(
            self):
        pass
    pass


class Examples(object):
    """
    Just a class quickly creating exemplary instances to play around with.
    """
    @staticmethod
    def pulse() -> Pulse:
        device = torch.device('cpu')
        dtype = torch.float32

        kw = {'dtype': dtype, 'device': device}
        N, nT, dt = 1, 512, dt0

        # pulse: Sec; Gauss; Gauss/cm.
        pulse_size = (N, 1, nT)
        t = torch.arange(0, nT, **kw).reshape(pulse_size)
        rf = 10*torch.cat([torch.cos(t/nT*2*π),                # (1,xy, nT)
                           torch.sin(t/nT*2*π)], 1)
        gr = torch.cat([torch.ones(pulse_size, **kw),
                        torch.ones(pulse_size, **kw),
                        10*torch.atan(t - round(nT/2))/π], 1)  # (1,xyz,nT)

        # Pulse
        print('Pulse(rf=rf, gr=gr, dt=gt, device=device, dtype=dtype)')
        return Pulse(rf=rf, gr=gr, dt=dt, **kw)

    @staticmethod
    def spincube() -> SpinCube:
        device = torch.device('cpu')
        dtype = torch.float32
        kw = {'dtype': dtype, 'device': device}

        N, Nd, γ_ = 1, (3, 3, 3), γH
        shape = (N, *Nd)
        mask = torch.zeros((1,)+Nd, device=device, dtype=torch.bool)
        mask[0, :, 1, :], mask[0, 1, :, :] = True, True
        fov, ofst = tensor([[3., 3., 3.]], **kw), tensor([[0., 0., 1.]], **kw)
        T1_, T2_ = tensor([[1.]], **kw), tensor([[4e-2]], **kw)

        cube = SpinCube(shape, fov, mask=mask, ofst=ofst,
                        T1_=T1_, T2_=T2_, γ_=γ_, **kw)

        cube.Δf = torch.sum(-cube.loc[0:1, :, :, :, 0:2], dim=-1) * γ_
        return cube
