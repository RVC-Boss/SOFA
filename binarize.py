import pathlib
import numpy as np
import torch
import yaml
from tqdm import tqdm
import pandas as pd
import h5py
import click
from modules.utils.load_wav import load_wav
from modules.utils.get_melspec import MelSpecExtractor


class ForcedAlignmentBinarizer:
    def __init__(self,
                 data_folder,
                 binary_data_folder,
                 valid_set_size,
                 valid_set_preferred_folders,
                 data_augmentation,
                 ignored_phonemes,
                 melspec_config,
                 max_frame_num,
                 ):

        self.data_folder = data_folder
        self.binary_data_folder = binary_data_folder
        self.valid_set_size = valid_set_size
        self.valid_set_preferred_folders = valid_set_preferred_folders
        self.data_augmentation = data_augmentation
        self.data_augmentation["key_shift_choices"] = np.array(self.data_augmentation["key_shift_choices"])
        self.ignored_phonemes = ignored_phonemes
        self.melspec_config = melspec_config
        self.max_frame_num = max_frame_num
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.sample_rate = self.melspec_config["sample_rate"]
        self.frame_length = self.melspec_config["hop_length"] / self.sample_rate

        self.get_melspec = MelSpecExtractor(**melspec_config, device=self.device)

    @staticmethod
    def get_vocab(data_folder_path, ignored_phonemes):
        print("Generating vocab...")
        phonemes = []
        trans_path_list = pathlib.Path(data_folder_path).rglob("transcriptions.csv")

        for trans_path in trans_path_list:
            if trans_path.name == "transcriptions.csv":
                df = pd.read_csv(trans_path)
                ph = list(set(" ".join(df["ph_seq"]).split(" ")))
                phonemes.extend(ph)

        phonemes = set(phonemes)
        for p in ignored_phonemes:
            if p in phonemes:
                phonemes.remove(p)
        phonemes = sorted(phonemes)
        phonemes = ["SP", *phonemes]

        vocab = dict(zip(phonemes, range(len(phonemes))))
        vocab.update(dict(zip(range(len(phonemes)), phonemes)))
        vocab.update({i: 0 for i in ignored_phonemes})
        vocab.update({"<vocab_size>": len(phonemes)})

        print(f"vocab_size is {len(phonemes)}")

        return vocab

    def process(self):
        vocab = self.get_vocab(self.data_folder, self.ignored_phonemes)
        with open(pathlib.Path(self.binary_data_folder) / "vocab.yaml", "w") as file:
            yaml.dump(vocab, file)

        # load metadata of each item
        meta_data_df = self.get_meta_data(self.data_folder)

        # split train and valid set
        valid_set_size = int(self.valid_set_size)
        meta_data_valid = (
            meta_data_df[meta_data_df["label_type"] != "no_label"]
            .sample(frac=1)
            .sort_values(by="preferred", ascending=False)
            .iloc[:valid_set_size, :]
        )
        meta_data_train = meta_data_df.drop(meta_data_valid.index).reset_index(drop=True)
        meta_data_valid = meta_data_valid.reset_index(drop=True)

        # binarize valid set
        self.binarize(
            "valid",
            meta_data_valid,
            vocab,
            self.binary_data_folder,
            False,
        )

        # binarize train set
        self.binarize(
            "train",
            meta_data_train,
            vocab,
            self.binary_data_folder,
            self.data_augmentation["size"] > 0,
        )

    def binarize(
            self,
            prefix: str,
            meta_data: pd.DataFrame,
            vocab: dict,
            binary_data_folder: str,
            enable_data_augmentation: bool,
    ):
        print(f"Binarizing {prefix} set...")
        meta_data["ph_seq"] = meta_data["ph_seq"].apply(
            lambda x: ([vocab[i] for i in x.split(" ")] if isinstance(x, str) else [])
        )
        meta_data["ph_dur"] = meta_data["ph_dur"].apply(
            lambda x: ([float(i) for i in x.split(" ")] if isinstance(x, str) else [])
        )
        meta_data = meta_data.sort_values(by="label_type").reset_index(drop=True)

        h5py_file_path = pathlib.Path(binary_data_folder) / (prefix + ".h5py")
        h5py_file = h5py.File(h5py_file_path, "w")
        h5py_meta_data = h5py_file.create_group("meta_data")
        items_meta_data = {"label_types": [], "wav_lengths": []}
        h5py_items = h5py_file.create_group("items")

        label_type_to_id = {"no_label": 0, "weak_label": 1, "full_label": 2}

        idx = 0
        total_time = 0.
        for _, item in tqdm(meta_data.iterrows(), total=meta_data.shape[0]):

            # input_feature: [data_augmentation.size+1,T,input_dim]
            waveform = load_wav(item.wav_path, self.device, self.sample_rate)
            input_feature = self.get_melspec(waveform)

            T = input_feature.shape[-1]
            if T > self.max_frame_num:
                print(f"Item {item.path} has a length of{T * self.max_frame_num} is too long, skip it.")
                continue
            else:
                h5py_item_data = h5py_items.create_group(str(idx))
                wav_length = T * self.frame_length
                items_meta_data["wav_lengths"].append(wav_length)
                idx += 1
                total_time += wav_length

            if enable_data_augmentation:
                input_features = [input_feature]
                key_shifts = np.random.choice(self.data_augmentation["key_shift_choices"],
                                              self.data_augmentation["size"],
                                              replace=False)
                for key_shift in key_shifts:
                    input_features.append(self.get_melspec(waveform, key_shift=key_shift))

                input_feature = torch.stack(input_features, dim=0)
            else:
                input_feature = input_feature.unsqueeze(0)

            input_feature = ((input_feature - input_feature.mean(dim=[1, 2], keepdim=True))
                             / input_feature.std(dim=[1, 2], keepdim=True)).permute(0, 2, 1)

            h5py_item_data["input_feature"] = input_feature.cpu().numpy().astype("float32")

            # label_type: []
            label_type_id = label_type_to_id[item.label_type]
            h5py_item_data["label_type"] = label_type_id
            items_meta_data["label_types"].append(label_type_id)

            if label_type_id < 1:
                # ph_seq: [S]
                ph_seq = np.array([]).astype("int32")

                # ph_edge: [2,T]
                ph_edge = np.zeros([2, T], dtype="float32")

                # ph_frame: [T]
                ph_frame = np.zeros(T, dtype="int32")
            elif label_type_id < 2:
                # ph_seq: [S]
                ph_seq = ph_seq.astype("int32")

                # ph_edge: [2,T]
                ph_edge = np.zeros([2, T], dtype="float32")
                # ph_frame: [T]
                ph_frame = np.zeros(T, dtype="int32")
            else:
                ph_seq = np.array(item.ph_seq).astype("int32")
                ph_dur = np.array(item.ph_dur).astype("float32")
                ph_time = (np.array(np.concatenate(([0], ph_dur))).cumsum() / self.frame_length)
                ph_interval = np.stack((ph_time[:-1], ph_time[1:]))

                ph_interval = ph_interval[:, ph_seq != 0]
                ph_seq = ph_seq[ph_seq != 0]
                ph_time = np.unique(ph_interval.flatten())

                # ph_seq: [S]
                ph_seq = ph_seq.astype("int32")

                # ph_edge: [2,T]
                ph_edge = np.zeros([2, T], dtype="float32")
                if ph_time[-1] + 0.5 > T:
                    ph_time = ph_time[:-1]
                if ph_time[0] - 0.5 < 0:
                    ph_time = ph_time[1:]
                ph_time_int = np.round(ph_time).astype("int32")
                ph_time_fractional = ph_time - ph_time_int

                ph_edge[0, ph_time_int] = 0.5 + ph_time_fractional
                ph_edge[0, ph_time_int - 1] = 0.5 - ph_time_fractional
                ph_edge[1, :] = 1 - ph_edge[0, :]

                # ph_frame: [T]
                ph_frame = np.zeros(T, dtype="int32")
                for ph_id, st, ed in zip(ph_seq, ph_interval[0], ph_interval[1]):
                    if st < 0:
                        st = 0
                    if ed > T:
                        ed = T
                    ph_frame[int(np.round(st)):int(np.round(ed))] = ph_id

            h5py_item_data["ph_seq"] = ph_seq.astype("int32")
            h5py_item_data["ph_edge"] = ph_edge.astype("float32")
            h5py_item_data["ph_frame"] = ph_frame.astype("int32")

            # print(h5py_item_data["input_feature"].shape,
            #       h5py_item_data["label_type"].shape,
            #       h5py_item_data["ph_seq"].shape,
            #       h5py_item_data["ph_edge"].shape,
            #       h5py_item_data["ph_frame"].shape
            #       )

        for k, v in items_meta_data.items():
            h5py_meta_data[k] = np.array(v)
        h5py_file.close()
        full_label_ratio = items_meta_data["label_types"].count(2) / len(items_meta_data["label_types"])
        weak_label_ratio = items_meta_data["label_types"].count(1) / len(items_meta_data["label_types"])
        no_label_ratio = items_meta_data["label_types"].count(0) / len(items_meta_data["label_types"])
        print(
            "Data compression ratio: \n"
            f"    full label data: {100 * full_label_ratio:.2f} %,\n"
            f"    weak label data: {100 * weak_label_ratio:.2f} %,\n"
            f"    no label data: {100 * no_label_ratio:.2f} %.")
        print(
            f"Successfully binarized {prefix} set, "
            f"total time {total_time:.2f}s, saved to {h5py_file_path}"
        )

    def get_meta_data(self, data_folder):
        path = pathlib.Path(data_folder)
        trans_path_list = [
            i for i in path.rglob("transcriptions.csv")
            if i.name == "transcriptions.csv"
        ]

        print("Loading metadata...")
        meta_data_df = pd.DataFrame()
        for trans_path in tqdm(trans_path_list):
            df = pd.read_csv(trans_path)
            df["wav_path"] = df["name"].apply(
                lambda name: str(trans_path.parent / "wavs" / (str(name) + ".wav")),
            )
            df["preferred"] = df["wav_path"].apply(
                lambda path_: (
                    True if any([i in pathlib.Path(path_).parts for i in self.valid_set_preferred_folders])
                    else False
                ),
            )
            df["label_type"] = df["wav_path"].apply(
                lambda path_: (
                    "full_label"
                    if "full_label" in path_
                    else "weak_label"
                    if "weak_label" in path_
                    else "no_label"
                ),
            )
            meta_data_df = pd.concat([meta_data_df, df])

        no_label_df = pd.DataFrame(
            {"wav_path": [i for i in (path / "no_label").rglob("*.wav")]}
        )
        meta_data_df = pd.concat([meta_data_df, no_label_df])
        meta_data_df["label_type"].fillna("no_label", inplace=True)

        meta_data_df.reset_index(drop=True, inplace=True)

        return meta_data_df


@click.command()
@click.option("--config_path", "-c", type=str, default="configs/binarize_config.yaml", show_default=True,
              help="binarize config path")
def binarize(config_path: str):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    global_config = {"binary_data_folder": config["binary_data_folder"],
                     "max_frame_num": config["max_frame_num"],
                     "melspec_config": config["melspec_config"],
                     "data_augmentation_size": config["data_augmentation"]["size"]}
    with open(pathlib.Path(config["binary_data_folder"]) / "global_config.yaml", "w") as file:
        yaml.dump(global_config, file)

    ForcedAlignmentBinarizer(**config).process()


if __name__ == "__main__":
    binarize()
