"""
音響特徴量可視化ツール

設定ファイルからDatasetCollectionを読み込み、フレーム単位で以下を可視化する:
- フレームF0
- 母音F0重心
- ボリューム
- 無音フラグ
- 音素区間

全ての系列はスペクトログラムのサンプリングレートに揃える。
音素区間と母音F0重心を重ねて表示し、音声再生にも対応する。
"""

import argparse
import tempfile
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
import japanize_matplotlib  # noqa: F401 日本語フォントに必須
import librosa
import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf
import yaml
from matplotlib.figure import Figure

from hiho_pytorch_base.config import Config
from hiho_pytorch_base.data.data import OutputData, create_frame_vowel_f0s
from hiho_pytorch_base.data.phoneme import ArpaPhoneme
from hiho_pytorch_base.dataset import (
    Dataset,
    DatasetCollection,
    DatasetType,
    LazyInputData,
    create_dataset,
)


@dataclass
class DataInfo:
    """データ情報"""

    phoneme_info: str
    speaker_id: str
    audio_path: str
    details: str


@dataclass
class FigureState:
    """図の状態"""

    main_plot_fig: Figure | None = None


def get_audio_path_from_lab(lab_file_path: Path) -> Path:
    """.labファイルのパスから対応する音声ファイルのパスを取得"""
    stem = lab_file_path.stem

    parts = stem.split("_")
    if len(parts) < 2:
        raise ValueError(f"無効なステム形式: {stem}")

    speaker_id = parts[0]
    chapter_id = parts[1]

    libritts_root = Path("/tmp/datasets/LibriTTS_clean_data/LibriTTS")
    audio_path = libritts_root / "dev-clean" / speaker_id / chapter_id / f"{stem}.wav"

    if not audio_path.exists():
        raise FileNotFoundError(f"音声ファイルが見つかりません: {audio_path}")

    return audio_path


def extract_audio_segment(audio_path: Path, start_time: float, end_time: float) -> str:
    """音声ファイルから指定時間範囲を切り出して一時ファイルとして保存"""
    try:
        audio, sr = librosa.load(str(audio_path), sr=None)

        start_sample = int(start_time * sr)
        end_sample = int(end_time * sr)
        requested_length = end_sample - start_sample

        if start_sample >= len(audio) or requested_length <= 0:
            raise ValueError(f"無効な時間範囲: {start_time} - {end_time}")

        actual_start = max(0, start_sample)
        actual_end = min(len(audio), end_sample)

        if actual_start < actual_end:
            audio_segment = audio[actual_start:actual_end]
        else:
            audio_segment = np.array([], dtype=audio.dtype)

        if len(audio_segment) < requested_length:
            padding_length = requested_length - len(audio_segment)
            padding = np.zeros(padding_length, dtype=audio.dtype)
            audio_segment = np.concatenate([audio_segment, padding])

        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(temp_file.name, audio_segment, sr)

        return temp_file.name

    except Exception as e:
        print(f"音声切り出しエラー: {e}")
        raise


class VisualizationApp:
    """可視化アプリケーション"""

    def __init__(self, config_path: Path, initial_dataset_type: DatasetType):
        self.config_path = config_path
        self.initial_dataset_type = initial_dataset_type

        self.dataset_collection = self._load_dataset()
        self.figure_state = FigureState()

    def _load_dataset(self) -> DatasetCollection:
        """データセットを読み込み"""
        with self.config_path.open() as f:
            config = Config.from_dict(yaml.safe_load(f))
        return create_dataset(config.dataset)

    def _get_dataset_and_data(
        self, index: int, dataset_type: DatasetType
    ) -> tuple[Dataset, OutputData, LazyInputData]:
        """データセットとデータを取得する共通処理"""
        dataset = self.dataset_collection.get(dataset_type)
        output_data = dataset[index]
        lazy_data = dataset.datas[index]
        return dataset, output_data, lazy_data

    def _get_file_info(self, index: int, dataset_type: DatasetType) -> str:
        """ファイル関連の情報テキストを取得"""
        dataset = self.dataset_collection.get(dataset_type)
        lazy_data = dataset.datas[index]

        try:
            audio_path = get_audio_path_from_lab(lazy_data.lab_path)
            audio_path_str = str(audio_path)
        except (FileNotFoundError, ValueError):
            audio_path_str = "見つからない"

        return f"""設定ファイル: {self.config_path}

F0データパス: {lazy_data.f0_path}
Volumeデータパス: {lazy_data.volume_path}
LABデータパス: {lazy_data.lab_path}
話者ID: {lazy_data.speaker_id}
音声ファイル: {audio_path_str}"""

    def _create_data_processing_text(
        self,
        *,
        frame_rate: float,
        input_f0_len: int,
        output_f0_len: int,
        volume_len: int,
        silence_len: int,
        spec_len: int,
        phoneme_count: int,
        speaker_id: int,
    ) -> str:
        """可視化対象のフレーム系列概要を表示する"""
        return (
            f"フレームレート: {frame_rate:.2f} Hz\n"
            f"フレームF0 shape: ({input_f0_len},)\n"
            f"母音F0重心 shape: ({output_f0_len},)\n"
            f"Volume(dB) shape: ({volume_len},)\n"
            f"Silence shape: ({silence_len},)\n"
            f"Spec frames: ({spec_len}, ?)\n"
            f"音素数: {phoneme_count}\n"
            f"話者ID: {speaker_id}"
        )

    def _create_f0_phoneme_plot(
        self,
        *,
        frame_rate: float,
        input_f0: np.ndarray,
        output_f0: np.ndarray,
        volume_db: np.ndarray,
        phonemes: list[ArpaPhoneme],
        time_start: float,
        time_end: float,
    ) -> Figure:
        """フレームF0/母音F0重心/Volume/音素区間の可視化を行う"""
        self.figure_state.main_plot_fig, ax1 = plt.subplots(1, 1, figsize=(24, 6))

        t = np.arange(len(input_f0)) / frame_rate

        # フレームF0（無声=0はNaNに）
        f0_in_disp = input_f0.astype(float).copy()
        f0_in_disp[f0_in_disp == 0] = np.nan
        ax1.plot(t, f0_in_disp, "b-", linewidth=2.5, label="フレームF0")
        ax1.set_xlabel("時間 (秒)", fontsize=20)
        ax1.set_ylabel("対数F0", fontsize=20, color="b")
        ax1.tick_params(axis="y", labelcolor="b")

        # 母音F0重心
        out_disp = output_f0.astype(float).copy()
        out_disp[out_disp == 0] = np.nan
        ax1.plot(
            t,
            out_disp,
            color="green",
            linestyle="--",
            linewidth=2.5,
            label="母音F0重心",
        )

        # Volume（第2軸）
        ax2 = ax1.twinx()
        ax2.plot(t, volume_db, "r-", linewidth=1.8, alpha=0.7, label="Volume(dB)")
        ax2.set_ylabel("Volume (dB)", fontsize=20, color="r")
        ax2.tick_params(axis="y", labelcolor="r")

        # 表示範囲
        ax1.set_xlim(time_start, time_end)
        ax2.set_xlim(time_start, time_end)

        # 音素区間ラベル
        y_min, y_max = ax1.get_ylim()
        cmap = plt.cm.tab20  # type: ignore
        max_phoneme_id = len(ArpaPhoneme.phoneme_list)
        for phoneme in phonemes:
            if phoneme.end >= time_start and phoneme.start <= time_end:
                color = cmap(phoneme.phoneme_id / max_phoneme_id)
                ax1.axvspan(phoneme.start, phoneme.end, alpha=0.18, color=color)
                mid_time = (phoneme.start + phoneme.end) / 2
                if time_start <= mid_time <= time_end:
                    ax1.text(
                        mid_time,
                        y_max - (y_max - y_min) * 0.1,
                        phoneme.phoneme,
                        ha="center",
                        va="top",
                        fontsize=15,
                        bbox=dict(boxstyle="round,pad=0.3", facecolor=color, alpha=0.6),
                    )

        # 凡例
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(
            lines1 + lines2,
            labels1 + labels2,
            fontsize=16,
            loc="lower right",
            ncol=3,
        )

        ax1.grid(True, alpha=0.3)
        ax1.tick_params(axis="both", which="major", labelsize=16)
        ax2.tick_params(axis="both", which="major", labelsize=16)

        plt.tight_layout()
        return self.figure_state.main_plot_fig

    def _create_spectrogram_plot(
        self,
        *,
        frame_rate: float,
        spec_data: np.ndarray,
        time_start: float,
        time_end: float,
    ) -> Figure:
        """スペクトログラムを可視化"""
        fig, ax = plt.subplots(1, 1, figsize=(24, 6))

        # 対数メルスペクトログラム
        # spec_dataは (time, freq) の形状を想定
        spec_db = spec_data.T

        # スペクトログラム表示
        ax.imshow(
            spec_db,
            aspect="auto",
            origin="lower",
            extent=(0, len(spec_data) / frame_rate, 0, spec_data.shape[1]),
            cmap="viridis",
            interpolation="none",
        )

        ax.set_xlabel("時間 (秒)", fontsize=20)
        ax.set_ylabel("メル周波数ビン", fontsize=20)
        ax.set_title("メルスペクトログラム", fontsize=22)
        ax.set_xlim(time_start, time_end)
        ax.tick_params(axis="both", which="major", labelsize=16)

        plt.tight_layout()
        return fig

    def _create_silence_plot(
        self,
        *,
        frame_rate: float,
        silence_flag: np.ndarray,
        time_start: float,
        time_end: float,
    ) -> Figure:
        """サイレンス情報を可視化"""
        fig, ax = plt.subplots(1, 1, figsize=(24, 2))

        # サイレンス背景塗り
        sil = silence_flag.astype(bool)
        if np.any(sil):
            in_region = False
            start_i = 0
            silence_regions = []

            # サイレンス区間を収集
            for i, v in enumerate(sil):
                if v and not in_region:
                    in_region = True
                    start_i = i
                elif not v and in_region:
                    in_region = False
                    silence_regions.append((start_i, i))
            if in_region:
                silence_regions.append((start_i, len(sil)))

            # 背景とラベルを描画（グレー色）
            for idx, (start_i, end_i) in enumerate(silence_regions):
                start_time = start_i / frame_rate
                end_time = end_i / frame_rate

                ax.axvspan(
                    start_time,
                    end_time,
                    color="gray",
                    alpha=0.7,
                    label="silence" if idx == 0 else None,
                )

        # 基準線を追加（非サイレンス区間を白で表示）
        ax.axhspan(0, 1, color="white", alpha=0.3, zorder=0)

        ax.set_xlim(time_start, time_end)
        ax.set_ylim(0, 1)
        ax.set_xlabel("時間 (秒)", fontsize=20)
        ax.set_ylabel("サイレンス", fontsize=20)
        ax.set_title("サイレンス区間", fontsize=22)
        ax.set_yticks([])
        ax.tick_params(axis="both", which="major", labelsize=16)
        ax.grid(True, alpha=0.3)

        # 凡例
        if np.any(sil):
            ax.legend(fontsize=16, loc="upper right")

        plt.tight_layout()
        return fig

    def _create_plots(
        self,
        index: int,
        dataset_type: DatasetType,
        time_start: float = 0,
        time_end: float = 2,
    ) -> tuple[Figure, Figure, Figure]:
        """プロットを作成"""
        _, _, lazy_data = self._get_dataset_and_data(index, dataset_type)

        input_data = lazy_data.fetch()
        phonemes = input_data.phonemes
        frame_rate = float(input_data.spec_data.rate)

        # リサンプリング: スペックのフレームレートに合わせる
        f0_in = input_data.f0_data.resample(sampling_rate=frame_rate, index=0)
        volume_db = input_data.volume_data.resample(sampling_rate=frame_rate, index=0)
        silence = input_data.silence_data.resample(sampling_rate=frame_rate, index=0)
        spec_len = int(len(input_data.spec_data.array))

        # 長さをスペックに合わせて統一
        frame_length = min(spec_len, len(f0_in), len(volume_db), len(silence))
        f0_in = f0_in[:frame_length]
        volume_db = volume_db[:frame_length]
        silence = silence[:frame_length]

        # 母音F0重心
        durations = np.array([p.duration for p in phonemes], dtype=np.float32)
        vowel_index = np.array(
            [i for i, p in enumerate(phonemes) if ArpaPhoneme.is_vowel(p.phoneme)]
        )
        output_f0 = create_frame_vowel_f0s(
            f0=f0_in,
            volume=volume_db,
            vowel_index=vowel_index,
            durations=durations,
            frame_rate=frame_rate,
        )

        # 長さを統一
        frame_length = min(frame_length, len(output_f0))
        f0_in = f0_in[:frame_length]
        volume_db = volume_db[:frame_length]
        silence = silence[:frame_length]
        output_f0 = output_f0[:frame_length]

        main_plot = self._create_f0_phoneme_plot(
            frame_rate=frame_rate,
            input_f0=f0_in,
            output_f0=output_f0,
            volume_db=volume_db,
            phonemes=phonemes,
            time_start=time_start,
            time_end=time_end,
        )

        # スペクトログラムプロット
        spec_array = input_data.spec_data.array[:frame_length]
        spectrogram_plot = self._create_spectrogram_plot(
            frame_rate=frame_rate,
            spec_data=spec_array,
            time_start=time_start,
            time_end=time_end,
        )

        # サイレンスプロット
        silence_plot = self._create_silence_plot(
            frame_rate=frame_rate,
            silence_flag=silence.astype(bool),
            time_start=time_start,
            time_end=time_end,
        )

        return main_plot, spectrogram_plot, silence_plot

    def _get_data_info(self, index: int, dataset_type: DatasetType) -> DataInfo:
        """データ情報を取得"""
        _, output_data, lazy_data = self._get_dataset_and_data(index, dataset_type)

        input_data = lazy_data.fetch()
        phonemes = input_data.phonemes
        frame_rate = float(input_data.spec_data.rate)

        # 音素情報
        phoneme_info_list = []
        for p in phonemes:
            info_line = f"  {p.phoneme}: {p.start:.3f}-{p.end:.3f}s"
            if ArpaPhoneme.is_vowel(p.phoneme) and p.stress is not None:
                info_line += f" [stress: {p.stress}]"
            phoneme_info_list.append(info_line)
        phoneme_info = "\n".join(phoneme_info_list)

        try:
            audio_path = get_audio_path_from_lab(lazy_data.lab_path)
            if audio_path:
                audio_path_str = str(audio_path)
            else:
                audio_path_str = "見つからない"
        except (FileNotFoundError, ValueError):
            audio_path_str = "見つからない"

        speaker_id = f"{output_data.speaker_id.item()}"

        file_info = self._get_file_info(index, dataset_type)

        # フレーム系列の長さをスペックのレートに合わせて算出（プロット処理は後段で実装）
        f0_in = input_data.f0_data.resample(sampling_rate=frame_rate, index=0)
        volume_db = input_data.volume_data.resample(sampling_rate=frame_rate, index=0)
        silence = input_data.silence_data.resample(sampling_rate=frame_rate, index=0)
        spec_len = int(len(input_data.spec_data.array))
        # 母音F0重心をフレーム系列に展開
        durations = np.array([p.duration for p in phonemes], dtype=np.float32)
        vowel_index = np.array(
            [i for i, p in enumerate(phonemes) if ArpaPhoneme.is_vowel(p.phoneme)]
        )
        output_f0 = create_frame_vowel_f0s(
            f0=f0_in,
            volume=volume_db,
            vowel_index=vowel_index,
            durations=durations,
            frame_rate=frame_rate,
        )
        frame_len = min(
            spec_len, len(f0_in), len(volume_db), len(silence), len(output_f0)
        )

        data_processing_info = self._create_data_processing_text(
            frame_rate=frame_rate,
            input_f0_len=frame_len,
            output_f0_len=frame_len,
            volume_len=frame_len,
            silence_len=frame_len,
            spec_len=spec_len,
            phoneme_count=len(phonemes),
            speaker_id=int(output_data.speaker_id.item()),
        )
        details = f"{file_info}\n\n--- データ処理結果 ---\n{data_processing_info}"

        return DataInfo(
            phoneme_info=phoneme_info,
            speaker_id=speaker_id,
            audio_path=audio_path_str,
            details=details,
        )

    def launch(self) -> None:
        """Gradio UIを起動"""
        initial_dataset = self.dataset_collection.get(self.initial_dataset_type)
        initial_max_index = len(initial_dataset) - 1

        with gr.Blocks() as demo:
            # 状態管理
            current_index = gr.State(0)
            current_dataset_type = gr.State(self.initial_dataset_type)

            # UI コンポーネント
            with gr.Row():
                dataset_type_dropdown = gr.Dropdown(
                    choices=list(DatasetType),
                    value=self.initial_dataset_type,
                    label="データセットタイプ",
                    scale=1,
                )
                index_slider = gr.Slider(
                    minimum=0,
                    maximum=initial_max_index,
                    value=0,
                    step=1,
                    label="サンプルインデックス",
                    scale=3,
                )

            # 状態管理
            current_time_start = gr.State(0.0)
            current_time_end = gr.State(2.0)

            @gr.render(
                inputs=[
                    current_index,
                    current_dataset_type,
                    current_time_start,
                    current_time_end,
                ]
            )
            def render_content(
                index: int,
                dataset_type: DatasetType,
                time_start: float,
                time_end: float,
            ):
                # プロットとデータ情報を取得
                main_plot, spectrogram_plot, silence_plot = self._create_plots(
                    index, dataset_type, time_start, time_end
                )
                data_info = self._get_data_info(index, dataset_type)

                # 音声取得を試みる
                try:
                    _, _, lazy_data = self._get_dataset_and_data(index, dataset_type)
                    audio_path = get_audio_path_from_lab(lazy_data.lab_path)
                    if audio_path:
                        audio_for_gradio = extract_audio_segment(
                            audio_path, time_start, time_end
                        )
                    else:
                        audio_for_gradio = None
                except Exception as e:
                    print(f"音声取得エラー: {e}")
                    audio_for_gradio = None

                with gr.Row():
                    time_start_input = gr.Number(
                        value=time_start, label="開始時間 (秒)", scale=1
                    )
                    time_end_input = gr.Number(
                        value=time_end, label="終了時間 (秒)", scale=1
                    )
                    left_btn = gr.Button("← 左へ", scale=1)
                    right_btn = gr.Button("右へ →", scale=1)

                # 時間範囲変更時の状態更新
                def update_time_range(new_start, new_end):
                    return new_start, new_end

                time_start_input.change(
                    update_time_range,
                    inputs=[time_start_input, time_end_input],
                    outputs=[current_time_start, current_time_end],
                )

                time_end_input.change(
                    update_time_range,
                    inputs=[time_start_input, time_end_input],
                    outputs=[current_time_start, current_time_end],
                )

                # 左右移動ボタンの機能
                def move_time_window(direction, current_start, current_end):
                    window_size = current_end - current_start
                    if direction == "left":
                        new_start = max(0, current_start - window_size * 1.0)
                    else:  # right
                        new_start = current_start + window_size * 1.0
                    new_end = new_start + window_size
                    return new_start, new_end, new_start, new_end

                left_btn.click(
                    lambda s, e: move_time_window("left", s, e),
                    inputs=[current_time_start, current_time_end],
                    outputs=[
                        current_time_start,
                        current_time_end,
                        time_start_input,
                        time_end_input,
                    ],
                )

                right_btn.click(
                    lambda s, e: move_time_window("right", s, e),
                    inputs=[current_time_start, current_time_end],
                    outputs=[
                        current_time_start,
                        current_time_end,
                        time_start_input,
                        time_end_input,
                    ],
                )

                with gr.Row():
                    if audio_for_gradio:
                        gr.Audio(value=audio_for_gradio, label="表示範囲の音声再生")
                    else:
                        gr.Audio(value=None, label="音声ファイルが見つかりません")

                with gr.Row():
                    gr.Plot(
                        value=main_plot,
                        label="フレームF0/母音F0重心/Volume/音素 可視化",
                    )

                with gr.Row():
                    gr.Plot(
                        value=spectrogram_plot,
                        label="メルスペクトログラム",
                    )

                with gr.Row():
                    gr.Plot(
                        value=silence_plot,
                        label="サイレンス区間",
                    )

                with gr.Row():
                    with gr.Column():
                        gr.Textbox(
                            value=data_info.speaker_id,
                            label="話者ID",
                            interactive=False,
                        )
                    with gr.Column():
                        gr.Textbox(
                            value=data_info.audio_path,
                            label="音声ファイル",
                            interactive=False,
                        )

                gr.Markdown("---")
                gr.Textbox(
                    value=data_info.phoneme_info,
                    label="音素区間情報",
                    lines=10,
                    max_lines=15,
                    interactive=False,
                )

                gr.Markdown("---")
                gr.Textbox(
                    value=data_info.details,
                    label="詳細情報",
                    lines=15,
                    max_lines=20,
                    interactive=False,
                )

            # UI操作から状態への更新
            index_slider.change(
                lambda new_index: new_index,
                inputs=[index_slider],
                outputs=[current_index],
            )

            def handle_dataset_change(new_type):
                dataset = self.dataset_collection.get(new_type)
                max_index = len(dataset) - 1
                return (
                    0,  # current_index
                    new_type,  # current_dataset_type
                    gr.update(value=0, maximum=max_index),  # スライダー
                )

            dataset_type_dropdown.change(
                handle_dataset_change,
                inputs=[dataset_type_dropdown],
                outputs=[current_index, current_dataset_type, index_slider],
            )

            # 初期化
            demo.load(
                lambda: (0, self.initial_dataset_type, 0.0, 2.0),
                outputs=[
                    current_index,
                    current_dataset_type,
                    current_time_start,
                    current_time_end,
                ],
            )

        demo.launch(share=False, server_name="0.0.0.0", server_port=7860)


def visualize(config_path: Path, dataset_type: DatasetType) -> None:
    """指定されたデータセットをGradio UIで可視化する"""
    app = VisualizationApp(config_path, dataset_type)
    app.launch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="F0予測データセットのビジュアライゼーション"
    )
    parser.add_argument("config_path", type=Path, help="設定ファイルのパス")
    parser.add_argument(
        "--dataset_type",
        type=DatasetType,
        default=DatasetType.TRAIN,
        help="データセットタイプ",
    )

    args = parser.parse_args()
    visualize(config_path=args.config_path, dataset_type=args.dataset_type)
