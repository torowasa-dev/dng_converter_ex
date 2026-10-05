"""Offline help content shared by the desktop guide and generated Markdown."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Paragraph:
    text: str
    heading: bool = False


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    weights: tuple[int, ...] = ()


@dataclass(frozen=True)
class HelpPage:
    key: str
    title: str
    blocks: tuple[Paragraph | Table, ...]


REFERENCES = (
    ("Adobe DNG Converter", "https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/adobe-dng-converter.html"),
    ("Adobe DNG仕様・SDK・CLI資料", "https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/digital-negative.html"),
    ("Adobe DNG Converter CLI仕様 PDF", "https://helpx.adobe.com/content/dam/help/en/camera-raw/digital-negative/jcr_content/root/content/flex/items/position/position-par/download_section/download-1/dng_converter_commandline.pdf"),
    ("libjxl：distance / effortの仕様", "https://libjxl.readthedocs.io/en/latest/api_encoder.html"),
    ("libjxl：ヒストグラムassertの既知報告 #3890", "https://github.com/libjxl/libjxl/issues/3890"),
    ("libjxl：サイズチェックの修正 #3897", "https://github.com/libjxl/libjxl/pull/3897"),
    ("Adobe SDK：JPEG XL既定設定の実装", "https://android.googlesource.com/platform/external/dng_sdk/%2B/68764928faa1d15f76bbf8f03c6e630c570a4354/source/dng_host.cpp"),
    ("Adobe SDK：RAW Proxy / Linear DNGの処理", "https://android.googlesource.com/platform/external/dng_sdk/%2B/de700ad461e35af50b28b861943a0b0753b10929/source/dng_negative.cpp"),
    ("Adobe：RAWとJPEGの明暗・WB編集の違い", "https://www.adobe.com/us/learn/lightroom-cc/web/raw-vs-jpeg"),
    ("Lightroom Classic：色温度とホワイトバランス", "https://helpx.adobe.com/jp/lightroom-classic/desktop/process-and-develop-photos/image-tone-color.html"),
)


HELP_PAGES = (
    HelpPage("recommendations", "おすすめ設定", (
        Paragraph("明暗の編集耐性を優先しながら、容量を減らす", heading=True),
        Paragraph("下表は比較を始めるための目安です。シャドウを強く持ち上げる写真や、ハイライトを大きく戻す写真では、高画質側から試してください。"),
        Table(("項目", "明暗の編集耐性を優先", "容量とのバランス"), (
            ("圧縮方式", "画質指定JPEG XL（Linear DNG）", "同左"),
            ("distance", "0.1（最高画質）", "0.3を比較 → 0.5（高画質）"),
            ("effort", "7（互換性を優先）", "7。安定動作を確認後に9を比較"),
            ("出力解像度", "原寸を維持", "必要な細部を残せるなら24 MP"),
            ("JPEGプレビュー", "なし", "なし"),
            ("Fast Load Data", "OFF（初期表示速度との交換）", "OFF"),
            ("元RAW埋め込み", "OFF（元RAWは別に保管）", "OFF"),
        ), (22, 40, 38)),
        Paragraph("付加データから減らす", heading=True),
        Paragraph("プレビュー・Fast Load Data・元RAWの埋め込みを減らすと、現像用の主画像に追加の画質劣化を与えず容量を抑えられます。実際の削減量は画像とAdobeの処理に依存します。"),
        Paragraph("完全保持を優先する写真は、原寸のロスレスDNGを選び、Linear化を無効にしてください。対応するモザイクRAWを保持できるかはAdobeの機種対応に従います。"),
        Paragraph("本アプリの初期設定", heading=True),
        Table(("distance", "effort", "解像度", "プレビュー", "Fast Load Data"), (
            ("0.1", "7", "原寸", "中サイズ", "ON"),
        )),
        Paragraph("推奨設定は自動で適用されません。「変換設定」と「詳細設定」で選択してください。強い補正後の階調や容量比を保証する数値ではありません。"),
    )),
    HelpPage("latitude", "画質と明暗", (
        Paragraph("JPEGの明暗情報が失われる理由", heading=True),
        Paragraph("通常のJPEGでは、現像時の階調処理・WB・白飛び・黒つぶれが画像に焼き込まれます。圧縮による誤差も加わります。JPEG XLに変えれば、失われた元RAWの情報が復元するという意味ではありません。"),
        Paragraph("RAWから直接、JPEG XL圧縮DNGに変換する", heading=True),
        Paragraph("本アプリの画質指定モードは、Adobeの処理でRAWをデモザイクし、Linear RAWとRAW用メタデータを持つDNGを出力します。現像済みのJPEG XL画像への書き出しとは用途が異なります。"),
        Table(("方式", "保存内容・編集の考え方"), (
            ("通常のJPEG", "現像済み画像。RAWと同じ明暗・WBの再調整には戻せない"),
            ("JPEG XL非可逆DNG", "通常の整数RAWでは16-bit Linear RAW。明暗・WBの再調整用"),
            ("原寸ロスレスDNG", "対応RAWの画素値の保持を優先。サイズ削減は画像次第"),
        ), (32, 68)),
        Paragraph("非可逆圧縮は、補正して初めて見える情報も変える可能性があります。保存ビット数が多いことは、元RAWの全情報・全編集機能が同じという保証ではありません。"),
        Paragraph("解像度と圧縮は別の損失", heading=True),
        Table(("処理", "残るもの・失われるもの"), (
            ("42 MP → 24 MP", "縦横比を維持して縮小。細部とトリミング余裕は減る"),
            ("distanceを小さくする", "圧縮誤差を抑える方向。縮小で失われた細部は戻らない"),
            ("distance = 0", "JPEG XL符号化段階はロスレス指定。Linear化・縮小は別"),
        ), (32, 68)),
        Paragraph("センサーで完全に飽和したハイライトなど、元RAWにない情報はDNGでも復元できません。重要な原本は別に保管し、非可逆DNGの再圧縮も避けてください。"),
    )),
    HelpPage("parameters", "distance / effort", (
        Paragraph("distanceは画質目標", heading=True),
        Paragraph("小さいほど高画質、大きいほど容量を抑える方向です。JPEG品質パーセントと一対一で換算しません。通常の表示で目立ちにくい誤差でも、強い明暗補正で見える場合があります。"),
        Table(("プリセット", "distance", "選び方の目安"), (
            ("最高画質", "0.1", "明暗の大きな補正を想定する出発点"),
            ("カスタム", "0.3", "最高画質と高画質の間で比較する候補"),
            ("高画質", "0.5", "画質と容量のバランスを比較"),
            ("標準", "1.0", "通常表示でも確認し、補正後も比較"),
            ("軽量 / 小容量 / 最小容量", "2.0 / 4.0 / 6.0", "容量優先。編集耐性を重視する場合は慎重に"),
            ("カスタム", "0", "符号化段階のロスレス。元モザイクRAWとの同一性とは別"),
        ), (30, 17, 53)),
        Paragraph("effortは計算量と圧縮効率", heading=True),
        Paragraph("大きいほど圧縮に時間をかけ、より効率のよい表現を探します。画質目標を直接決める設定ではありません。同じdistanceでも容量や復号画素が完全に同じとは限らず、必ず小さくなる保証もありません。"),
        Table(("effort", "選び方"), (
            ("1", "変換速度を優先"),
            ("7", "本アプリの初期値。既知assertが出る環境で最初に試す"),
            ("9", "変換時間をかけ、圧縮効率を優先。安定動作を確認して選択"),
        ), (20, 80)),
        Paragraph("effort自体はデコード速度を指定する値ではありません。本アプリの入力範囲はAdobe CLIの範囲です。ロスレスJPEG XLモードはAdobeの固定effortを使用します。"),
        Table(("項目", "本アプリの仕様"), (
            ("画質指定モードの入力範囲", "distance: 0〜6 / effort: 1〜9"),
            ("ロスレスJPEG XL", "Adobeの -losslessJXL を使用。distance 0 / effort 7"),
        ), (40, 60)),
    )),
    HelpPage("adobe", "Adobeとの比較", (
        Paragraph("Adobe DNG Converterの非可逆DNGと比較する", heading=True),
        Paragraph("本アプリはインストール済みAdobe DNG Converterを呼び出します。画質指定モードではdistanceとeffortを明示します。Adobe GUIの全補助機能を再実装したものではありません。"),
        Paragraph("Adobe SDKの公開実装から読み取れる目安", heading=True),
        Table(("条件", "distance", "effort"), (
            ("通常の整数RAWのEncoded Main Image", "0.5", "7"),
            ("整数RAWのProxy Image：出力が5 MP以上", "0.5", "7"),
            ("整数RAWのProxy Image：出力が5 MP未満", "1.0", "7"),
            ("本アプリの初期設定", "0.1", "7"),
        ), (65, 18, 17)),
        Paragraph("これは参照先のSDK実装に基づく推定です。各バージョンのAdobe DNG Converter GUIで実測した既定値ではありません。HDR / 浮動小数点RAW、特殊な処理、旧方式の非可逆JPEGには、この比較をそのまま適用できません。"),
        Paragraph("Adobe相当の数値と、完全な同一出力は別", heading=True),
        Paragraph("同じdistance・effortでも、Converterのバージョンや処理経路で結果が変わる可能性があります。入力RAW・解像度・プレビュー・埋め込み設定も揃えて比較してください。"),
        Paragraph("旧方式の非可逆JPEGモードは画質固定です。JPEG XLのdistanceでは、そのJPEG圧縮の画質を指定できません。"),
        Paragraph("資料の確認日：2026-10-02。参照したSDKのコミットは参考資料のURLで固定しています。"),
    )),
    HelpPage("lightroom", "Lightroomで確認", (
        Paragraph("取り込みは手動で行います", heading=True),
        Paragraph("本アプリで変換・保存したDNGを、Lightroomから取り込んでください。元RAWとDNGに同じプロファイル・WB・現像設定を適用し、差を確認します。"),
        Table(("比較する内容", "確認方法"), (
            ("WB / 色温度", "Kelvinで入力できるか。同じ値で色が揃うか"),
            ("シャドウ", "露光量・シャドウを強く上げ、階調・色・細部を見る"),
            ("ハイライト", "露光量・ハイライトを下げ、明部の階調・色を見る"),
            ("細部", "同じ出力寸法で比較。縮小差と圧縮差を分けて見る"),
            ("容量", "埋め込みプレビューではなく主画像の画質と併せて評価"),
        ), (24, 76)),
        Paragraph("アプリの「選択RAWの画質を比較」では、入力一覧でファイルを一つ選び、複数のdistanceを指定します。解像度とeffortは共通設定のまま、画質ごとのフォルダーにDNGを出力します。"),
        Table(("比較用の入力例", "表示・出力の扱い"), (
            ("0.1,0.3,0.5", "quality_d0p1 / quality_d0p3 / quality_d0p5"),
            ("100%表示", "圧縮による色のにじみ・階調・細部を確認"),
        ), (30, 70)),
        Paragraph("本アプリが検査する範囲", heading=True),
        Paragraph("RAW画像の構造、圧縮方式、寸法、色行列、WBタグを検査します。画像の復号、実画質、Lightroom上のKelvin操作、指定distanceの内部使用までを実証する検査ではありません。"),
        Paragraph("Linear DNGでは、元のモザイクRAWと利用できる機能が異なる場合があります。Rawディテール等の強化機能・専用プロファイル・メーカー独自情報はAdobe側と出力形式の対応に依存します。"),
        Table(("JPEG XLの動作対象", "本アプリが対象にするバージョン"), (
            ("Adobe DNG Converter", "16以降"),
            ("Lightroom Classic / Camera Raw", "13以降 / 16以降（保守的な対象設定）"),
        ), (42, 58)),
    )),
    HelpPage("recovery", "JPEG XLエラー対策", (
        Paragraph("画質目標と解像度を維持して回避を試す", heading=True),
        Paragraph("特定のヒストグラムassertに限り、effortを下げて一度再試行します。Adobe内蔵版で同じ不具合と確定したものではなく、回避成功を保証しません。"),
        Table(("項目", "処理"), (
            ("対象エラー", "Adobeが異常終了し、enc_ans.cc / JXL_DASSERT / n <= 255が同じ行にある"),
            ("初期状態", "自動再試行ON、ロスレスJPEGへの代替出力OFF"),
            ("effort再試行", "画質指定JPEG XLでeffort 8/9の場合のみ、7に変更して一度再試行"),
            ("維持する設定", "distance、MP／長辺、WB必須、プレビュー、元RAW埋め込み等"),
            ("制限時間", "ファイル単位。全試行を合算し、中止操作も継続して有効"),
            ("結果確認", "要求設定、成功時の設定、各試行の終了コード・ログ末尾・検査結果を記録"),
        ), (25, 75)),
        Paragraph("詳細設定の「互換性優先：effort 7に設定」は、effortと自動再試行だけを変更します。画質と解像度は変更しません。容量が増える場合があり、同じdistanceでも復号画素の完全一致は保証しません。"),
        Paragraph("代替出力は原寸指定時だけ", heading=True),
        Table(("設定", "該当assertが残る場合"), (
            ("代替出力OFF", "エラーを記録し、次の入力ファイルへ進む"),
            ("代替出力ON・原寸", "元の入力RAWからロスレスJPEG圧縮DNGへ変更。形式変更を結果に表示"),
            ("MP／長辺の指定あり", "代替出力は選択不可。縮小指定を勝手に解除しない"),
            ("ロスレスJPEG XL", "GUIのeffort指定はAdobeへ渡らない。effort再試行はせず、選択時だけ代替出力"),
        ), (32, 68)),
        Paragraph("各試行は元の入力から新しいAdobeプロセス・一時フォルダーで実行します。途中DNGを再利用せず、終了とメタデータ検査に成功した出力だけ保存します。失敗時の途中DNGの再圧縮や、旧方式の非可逆JPEGへの自動変更は行いません。"),
        Paragraph("既知報告と対策の限界", heading=True),
        Paragraph("libjxl #3890は16bit単色・可逆圧縮・effort 8以上での再現報告です。1×1画素でも再現するため、解像度を下げること自体は確実な対策ではありません。effort 7でも失敗する場合のeffort 5は、手動で試す候補にとどめます。"),
        Paragraph("根本対処には、修正を含むAdobe DNG Converterが必要です。Adobeへの修正取り込み状況は未確認です。Pythonパッケージの更新やassertの無効化では、このサイズチェック修正の代替になりません。"),
    )),
    HelpPage("usage", "使い方・資料", (
        Paragraph("変換の手順", heading=True),
        Table(("順番", "操作"), (
            ("1", "Adobe DNG Converterを別途インストール"),
            ("2", "実行ファイルを自動検出、または参照して指定"),
            ("3", "RAWファイル・フォルダーを追加し、出力先を選択"),
            ("4", "圧縮方式・画質・解像度・詳細設定を選び、変換を開始"),
            ("5", "結果を確認し、Lightroomへの取り込みは手動で行う"),
        ), (12, 88)),
        Paragraph("画素数を減らす場合の設定例", heading=True),
        Table(("項目", "42 MP → 24 MPの例"), (
            ("圧縮方式", "画質指定JPEG XL（Linear DNG）"),
            ("出力解像度", "画素数で指定（MP）"),
            ("目標画素数", "24（Adobeには24,000,000画素を指定）"),
        ), (30, 70)),
        Paragraph("縦横比を維持し、拡大はしません。目標は上限で、整数寸法への丸めにより画素数は厳密には一致しない場合があります。"),
        Paragraph("結果行をダブルクリックすると、要求設定・成功時の設定・各試行の実行コマンドと検査情報を表示します。失敗したファイルを記録して次に進み、中止や制限時間超過では未完了の一時出力を破棄します。"),
        Paragraph("原本保護と動作対象", heading=True),
        Paragraph("入力RAWを削除・変更しません。初期状態では同名出力に連番を付けます。Adobeの公式変換はWindows / macOSが対象で、LinuxでAdobeによるネイティブ変換を行う機能はありません。"),
        Paragraph("参考資料", heading=True),
        Paragraph("以下は公式資料とAdobe SDKの公開実装です。本文はオフラインで読めます。参考リンクを開くとブラウザーを使用します。"),
    )),
)


def markdown_guide() -> str:
    """Export exactly the guide content, so the app and repository stay aligned."""
    parts = ["# 画質ガイド", "アプリの「画質ガイド / F1」と同じ説明です。推奨値は比較開始の目安であり、実画質や容量の保証値ではありません。"]
    for page in HELP_PAGES:
        parts.append("## " + page.title)
        for block in page.blocks:
            if isinstance(block, Paragraph):
                parts.append(("### " if block.heading else "") + block.text)
            else:
                def row(values):
                    return "| " + " | ".join(value.replace("|", "\\|").replace("\n", "<br>") for value in values) + " |"
                parts.append("\n".join([row(block.headers), row(tuple("---" for _ in block.headers)), *(row(values) for values in block.rows)]))
    parts.extend(("## 参考資料", "\n".join(f"- [{title}]({url})" for title, url in REFERENCES)))
    return "\n\n".join(parts) + "\n"
