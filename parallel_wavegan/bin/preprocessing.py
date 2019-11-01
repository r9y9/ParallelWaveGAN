#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2019 Tomoki Hayashi
#  MIT License (https://opensource.org/licenses/MIT)

"""Perform preprocessing for the training of WaveGAN."""

import argparse
import logging
import os

import kaldiio
import librosa
import numpy as np
import soundfile as sf
import yaml

from joblib import delayed
from joblib import Parallel
from tqdm import tqdm

from parallel_wavegan.datasets import AudioDataset

# make sure each process use single thread
os.environ["OMP_NUM_THREADS"] = "1"


def logmelfilterbank(x,
                     sampling_rate,
                     fft_size=1024,
                     hop_size=256,
                     win_length=None,
                     window="hann",
                     num_mels=80,
                     fmin=None,
                     fmax=None,
                     eps=1e-10,
                     ):
    """Compute log-Mel filterbank feature."""
    # get amplitude spectrogram
    x_stft = librosa.stft(x, n_fft=fft_size, hop_length=hop_size,
                          win_length=win_length, window=window, pad_mode="reflect")
    spc = np.abs(x_stft).T  # (#frames, #bins)

    # get mel basis
    fmin = 0 if fmin is None else fmin
    fmax = sampling_rate / 2 if fmax is None else fmax
    mel_basis = librosa.filters.mel(sampling_rate, fft_size, num_mels, fmin, fmax)

    return np.log10(np.maximum(eps, np.dot(spc, mel_basis.T)))


def main():
    """Run preprocessing process."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--wavscp", default=None, type=str,
                        help="Kaldi-style wav.scp file.")
    parser.add_argument("--segments", default=None, type=str,
                        help="Kaldi-style segments file.")
    parser.add_argument("--rootdir", default=None, type=str,
                        help="Directory including wav files.")
    parser.add_argument("--outdir", default=None, type=str,
                        help="Direcotry to save checkpoints.")
    parser.add_argument("--config", default="hparam.yml", type=str,
                        help="Yaml format configuration file.")
    parser.add_argument("--verbose", type=int, default=1,
                        help="logging level (higher is more logging)")
    parser.add_argument("--n_jobs", type=int, default=16,
                        help="Number of parallel jobs.")
    args = parser.parse_args()

    # set logger
    if args.verbose > 1:
        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s")
    elif args.verbose > 0:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s")
    else:
        logging.basicConfig(
            level=logging.WARN, format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s")
        logging.warning('skip DEBUG/INFO messages')

    # load config
    with open(args.config) as f:
        config = yaml.load(f, Loader=yaml.Loader)
    config.update(vars(args))

    # get reader
    if args.wavscp is not None:
        reader = kaldiio.ReadHelper(f"scp:{args.wavscp}",
                                    segments=args.segments)
    else:
        reader = AudioDataset(args.rootdir, "*.wav",
                              audio_load_fn=sf.read,
                              return_filename=True)

    # check directly existence
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir, exist_ok=True)

    # define function for parallel processing
    def _process_single_file(name, data):
        # parse inputs
        if args.wavscp is not None:
            utt_id = name
            fs, x = data
            x = x.astype(np.float32)
            x /= (1 << (16 - 1))  # assume that wav is PCM 16 bit
        else:
            utt_id = os.path.basename(name).replace(".wav", "")
            x, fs = data

        # check
        assert len(x.shape) == 1, \
            f"{utt_id} seems to be multi-channel signal."
        assert fs == config["sampling_rate"], \
            f"{utt_id} seems to have a different sampling rate."
        assert np.abs(x).max() <= 1.0, \
            f"{utt_id} seems to be different from 16 bit PCM."

        # extract feature
        feats = logmelfilterbank(x, fs,
                                 fft_size=config["fft_size"],
                                 hop_size=config["hop_size"],
                                 win_length=config["win_length"],
                                 window=config["window"],
                                 num_mels=config["num_mels"],
                                 fmin=config["fmin"],
                                 fmax=config["fmax"])

        # make sure the audio length and feature length are matched
        x = np.pad(x, (0, config["fft_size"]), mode="edge")
        x = x[:len(feats) * config["hop_size"]]
        assert len(feats) * config["hop_size"] == len(x)

        # apply global gain
        if config["global_gain_scale"] > 0.0:
            x *= config["global_gain_scale"]
            if np.abs(x).max() > 1.0:
                logging.warn(f"{utt_id} causes clipping. "
                             f"it is better to re-consider global gain scale.")
                return

        # save
        np.save(os.path.join(args.outdir, f"{utt_id}-wave.npy"),
                x.astype(np.float32), allow_pickle=False)
        np.save(os.path.join(args.outdir, f"{utt_id}-feats.npy"),
                feats.astype(np.float32), allow_pickle=False)

    # process in parallel
    Parallel(n_jobs=args.n_jobs, verbose=args.verbose)(
        [delayed(_process_single_file)(name, data) for name, data in tqdm(reader)])


if __name__ == "__main__":
    main()
