# Z-Imageワークフロー

API形式のテンプレートです。管理対象の入力だけを `_chotto_sashie_bindings` で宣言しています。このメタデータはComfyUIへの送信前に除去されます。

| ファイル | 用途 |
|---|---|
| `zimage_basic_core.json` | 背景・小物の生成（Z-Image-Turbo、標準ノードだけ） |
| `zimage_candidate_core.json` | 人物候補の生成（Z-Image-Turbo） |
| `qwen_character_reference_2511_angles_external.json` | 基準画像から立ち絵を作る（Qwen-Image-Edit-2511＋カメラ角度LoRA、`TextEncodeQwenImageEditPlus` と参照潜在方式） |

立ち絵のテンプレートは2511専用です。2509用のエンコード（`TextEncodeQwenImageEdit`）のグラフへ2511モデルだけを載せると、人物が二人になったり座り姿など別構図へ崩れます。外部ComfyUIでは、投入前に不足ノードとモデルを検査します。
