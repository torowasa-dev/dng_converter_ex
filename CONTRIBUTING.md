# 開発と検証

アプリの変換はAdobe DNG Converterが行います。Linuxでは構造検査・模擬変換・GUI表示を開発用に確認できます。
実RAW変換の確認にはWindows / macOSとAdobe DNG Converterが必要です。

## 基本チェック

リポジトリのルートで実行します。

```console
python -m unittest discover -s tests -v
python scripts/export_quality_guide.py --check
python app.py doctor
```

`doctor`はAdobeが見つからないと非ゼロで終了します。Adobeをインストールしていない開発環境では、環境不足の確認として扱ってください。
合成DNGの圧縮ペイロードはダミーです。実画質の検証やLightroom互換の証拠には使用できません。

## 画質ガイドの変更

説明の原本は `raw_to_dng/help_content.py` です。GUIも同じデータを使用します。
変更後、以下で文書を生成します。推奨値、SDKからの推定、実機検証済みの事実を区別してください。

```console
python scripts/export_quality_guide.py
```

## GUIの確認とスクリーンショット

撮影スクリプトは、実際のTkinter画面を表示してウィンドウを撮影します。
サンプルのパスと設定を表示しますが、Adobeを呼び出したり、RAWを変換したりはしません。
変換結果や容量を合成したスクリーンショットではありません。
通常のアプリ実行には不要なPillowを、開発用に追加します。

```console
python -m pip install Pillow
python scripts/capture_screenshots.py
```

Linuxの画面がない環境では、Xvfb、日本語フォント、Tkinterが必要です。Ubuntuの例を示します。

```console
sudo apt-get install xvfb fonts-noto-cjk python3-tk
xvfb-run -a -s "-screen 0 1600x1200x24" python scripts/capture_screenshots.py
```

| 出力 | 内容 |
|---|---|
| `docs/screenshots/main.png` | 変換設定。画素数を指定する設定例 |
| `docs/screenshots/advanced.png` | プレビュー、effortなどの詳細設定 |
| `docs/screenshots/quality-guide.png` | おすすめ設定のガイド |
| `docs/screenshots/parameters.png` | distance / effortのガイド |

撮影時は一時設定を使用し、通常利用の設定ファイルに書き込みません。
ヘルプの再利用・再表示・全ページ・スクロールと、変換設定との分離もスクリプト内で確認します。
Windows / macOSの撮影はPillowとOSの画面キャプチャ機能・許可に依存します。
README画像を更新する場合は、撮影OSと検証範囲の説明も合わせて更新してください。

## 実機での確認

| 対象 | 確認内容 |
|---|---|
| コンバーター | バージョン、各圧縮モード、指定した画素数・長辺 |
| 原本保護 | 入力の不変、同名ファイル、途中中止・時間切れ |
| Lightroom | 手動読み込み、プロファイル、Kelvin入力、明暗の強い補正 |
| 実画質 | 同じ寸法・現像設定で比較。縮小と圧縮の差を分ける |
| 容量 | プレビュー・RAW埋め込み・ノイズ等の設定も記録 |
| EXE | Windowsでビルドし、Adobeが別途必要であることを確認 |

本リポジトリにはAdobeの実行ファイル・SDKコード・カメラプロファイルを追加しないでください。
