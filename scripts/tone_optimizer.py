"""Closed-loop headless tone optimiser (`improvements.md` Engine B.3).

Replaces the broken open-loop formulas in ``tone_design.py``
(``cutoff = centroid / 0.18`` with zero render-and-compare feedback, measured to
produce 9/12 silent keys and +990..+1120 cents sharp). Here a candidate patch is
**actually rendered** through the synth in `pedalboard` and scored against clean
reference clips from the stem, then a search (Optuna TPE, or scipy Powell /
Nelder-Mead when Optuna is unavailable) nudges 8-12 high-impact continuous
parameters to minimise a composite perceptual loss:

    L = w1*L_mrstft + w2*L_mfcc + w3*L_env + w4*L_pitch_stability

Hard guards (Section 6 Engine B.3): any candidate with silent keys,
peak < -30 dBFS, or pitch error > 25 cents is rejected outright.

The optimiser is engine-agnostic: it only needs a ``render_fn(params) -> audio``
callable and a parameter space, so the same search drives Vital (Engine B) and
Surge XT (Engine C).
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tone_common as tc  # noqa: E402

CFG = {
    # Composite loss weights (Section 6 Engine B.3).
    "w_mrstft": 1.0, "w_mfcc": 0.6, "w_env": 0.5, "w_pitch": 1.2,
    # Multi-resolution STFT window sizes.
    "mrstft_ffts": (512, 1024, 2048),
    # Hard guards.
    "silent_peak_dbfs": -30.0,
    "pitch_err_cents": 25.0,
    # Search budget.
    "n_iters": 30,
}


# ---------------------------------------------------------------- losses

def _stft_mag(y, n_fft):
    import librosa
    return np.abs(librosa.stft(np.asarray(y, dtype=np.float64),
                               n_fft=n_fft, hop_length=n_fft // 4))


def loss_mrstft(ref, est):
    """Multi-resolution STFT magnitude L1 (log-magnitude)."""
    import librosa
    ref = np.asarray(ref, dtype=np.float64)
    est = np.asarray(est, dtype=np.float64)
    n = min(len(ref), len(est))
    ref, est = ref[:n], est[:n]
    total = 0.0
    for n_fft in CFG["mrstft_ffts"]:
        R = np.log1p(_stft_mag(ref, n_fft))
        E = np.log1p(_stft_mag(est, n_fft))
        m = min(R.shape[1], E.shape[1])
        total += float(np.mean(np.abs(R[:, :m] - E[:, :m])))
    return total / len(CFG["mrstft_ffts"])


def loss_mfcc(ref, est):
    import librosa
    n = min(len(ref), len(est))
    ref, est = ref[:n], est[:n]
    R = librosa.feature.mfcc(y=ref, sr=tc.SR, n_mfcc=13)
    E = librosa.feature.mfcc(y=est, sr=tc.SR, n_mfcc=13)
    m = min(R.shape[1], E.shape[1])
    return float(np.mean(np.abs(R[:, :m] - E[:, :m])))


def loss_env(ref, est):
    import librosa
    n = min(len(ref), len(est))
    R = librosa.feature.rms(y=ref[:n], hop_length=512)[0]
    E = librosa.feature.rms(y=est[:n], hop_length=512)[0]
    m = min(len(R), len(E))
    R, E = R[:m], E[:m]
    R = R / (R.max() + 1e-9)
    E = E / (E.max() + 1e-9)
    return float(np.mean(np.abs(R - E)))


def loss_pitch(ref, est, target_midi=None):
    """Penalise pitch error of the render vs. its intended MIDI key."""
    f0 = tc.estimate_f0(est, tc.SR)
    if f0 is None or target_midi is None:
        return 0.0
    err = abs(tc.cents_between(tc.midi_to_hz(target_midi), f0))
    return float(max(0.0, err - CFG["pitch_err_cents"]) / 100.0)


def composite_loss(ref, est, target_midi=None):
    if est is None or np.max(np.abs(est)) < tc.from_db(CFG["silent_peak_dbfs"]):
        return 1e6                      # silent / failed render: hard reject
    w = CFG
    return (w["w_mrstft"] * loss_mrstft(ref, est)
            + w["w_mfcc"] * loss_mfcc(ref, est)
            + w["w_env"] * loss_env(ref, est)
            + w["w_pitch"] * loss_pitch(ref, est, target_midi))


# ---------------------------------------------------------------- search

def _search_optuna(objective, space, n_iters):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=1234))
    def _obj(trial):
        params = {name: trial.suggest_float(name, lo, hi)
                  for (name, lo, hi) in space}
        return objective(params)
    study.optimize(_obj, n_trials=n_iters)
    return study.best_params, [t.value for t in study.trials if t.value is not None]


def _search_scipy(objective, space, n_iters):
    from scipy.optimize import minimize
    names = [s[0] for s in space]
    x0 = np.array([(lo + hi) / 2.0 for (_n, lo, hi) in space])
    bounds = [(lo, hi) for (_n, lo, hi) in space]
    curve = []

    def _f(x):
        params = {name: float(v) for name, v in zip(names, x)}
        val = objective(params)
        curve.append(val)
        return val

    res = minimize(_f, x0, method="Nelder-Mead",
                   options={"maxiter": n_iters, "xatol": 1e-2, "fatol": 1e-3})
    best = {name: float(v) for name, v in zip(names, res.x)}
    return best, curve


def optimize(render_fn, reference, space, n_iters=CFG["n_iters"],
             target_midi=None, verbose=False):
    """Minimise `composite_loss(reference, render_fn(params))` over `space`.

    `space` is a list of (name, lo, hi). `render_fn(params_dict) -> 1-D audio`.
    Returns (best_params, loss_curve, best_loss).
    """
    def objective(params):
        try:
            est = render_fn(params)
        except Exception:
            return 1e6
        return composite_loss(reference, est, target_midi=target_midi)

    try:
        best, curve = _search_optuna(objective, space, n_iters)
    except Exception:
        best, curve = _search_scipy(objective, space, n_iters)
    best_loss = objective(best)
    if verbose:
        print(f"    optimizer: best_loss={best_loss:.4f} iters={len(curve)}")
    return best, curve, best_loss
