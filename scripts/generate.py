"""学習済みモデルを用いた生成スクリプト"""

import argparse
import re
from pathlib import Path

import yaml
from torch.utils.data import DataLoader
from tqdm import tqdm

from hiho_pytorch_base.batch import BatchOutput, collate_dataset_output
from hiho_pytorch_base.config import Config
from hiho_pytorch_base.dataset import DatasetType, create_dataset
from hiho_pytorch_base.generator import Generator
from scripts.utility.save_arguments import save_arguments


def _extract_number(f):
    s = re.findall(r"\d+", str(f))
    return int(s[-1]) if s else -1


def _get_predictor_model_path(
    model_dir: Path, iteration: int | None = None, prefix: str = "predictor_"
):
    if iteration is None:
        paths = model_dir.glob(prefix + "*.pth")
        model_path = list(sorted(paths, key=_extract_number))[-1]
    else:
        model_path = model_dir / (prefix + f"{iteration}.pth")
        assert model_path.exists()
    return model_path


def generate(
    model_dir: Path | None,
    predictor_iteration: int | None,
    config_path: Path | None,
    predictor_path: Path | None,
    dataset_type: DatasetType,
    output_dir: Path,
    use_gpu: bool,
):
    """設定にあるデータセットから生成する"""
    if predictor_path is None and model_dir is not None:
        predictor_path = _get_predictor_model_path(
            model_dir=model_dir, iteration=predictor_iteration
        )
    else:
        raise ValueError("predictor_path または model_dir のいずれかを指定してください")

    if config_path is None and model_dir is not None:
        config_path = model_dir / "config.yaml"
    else:
        raise ValueError("config_path または model_dir のいずれかを指定してください")

    output_dir.mkdir(exist_ok=True)
    save_arguments(output_dir / "arguments.yaml", generate, locals())

    with config_path.open() as f:
        config = Config.from_dict(yaml.safe_load(f))

    generator = Generator(config=config, predictor=predictor_path, use_gpu=use_gpu)

    dataset = create_dataset(config.dataset).get(dataset_type)
    data_loader = DataLoader(
        dataset=dataset,
        batch_size=1,
        shuffle=False,
        collate_fn=collate_dataset_output,
    )

    batch: BatchOutput
    for batch in tqdm(data_loader, desc="generate"):
        batch.to_device(device="cuda" if use_gpu else "cpu", non_blocking=True)
        _ = generator(
            f0_list=batch.f0_list,
            phoneme_list=batch.phoneme_list,
            speaker_id=batch.speaker_id,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", type=Path)
    parser.add_argument("--predictor_iteration", type=int)
    parser.add_argument("--config_path", type=Path)
    parser.add_argument("--predictor_path", type=Path)
    parser.add_argument("--dataset_type", type=DatasetType, required=True)
    parser.add_argument("--output_dir", required=True, type=Path)
    parser.add_argument("--use_gpu", action="store_true")
    generate(**vars(parser.parse_args()))
