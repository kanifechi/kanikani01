# 思考整理ボード（Android アプリ版）

1 枚の HTML（`app/src/main/assets/www/index.html`）をそのまま内蔵した Android アプリです。
インストールするとホーム画面にアイコンが並び、ブラウザの URL バーやタブが出ない
「ふつうのアプリ」として起動します。通信がなくても動きます。

## 入手してインストールする

1. GitHub の **Actions → Android APK** で最新のビルドを開く
   - 作業ブランチのビルド → ページ下部の **Artifacts** から `shikoboard-apk` をダウンロード（zip なので展開する）
   - `main` にマージ済み → **Releases → android-latest** の `shikoboard.apk` を直接ダウンロードできる（スマホから開くならこちらが楽）
2. スマホで `shikoboard.apk` をタップする
3. 「この提供元のアプリを許可」を求められたら許可する
   （Play ストア以外からのインストールなので初回だけ確認が出ます）
4. ホーム画面／アプリ一覧に **思考整理ボード** が追加される

## データについて

- 入力内容はアプリ内の `localStorage` に保存されます。**ブラウザで作ったデータは引き継がれません。**
  移すときは、ブラウザ側で「保存」して JSON を書き出し、アプリ側の「読み込み」で取り込んでください。
- アプリをアンインストールするとデータも消えます。ときどき「保存」で JSON を書き出しておくのが安全です。
- 書き出したファイルは端末の **Download** フォルダに入ります。

## 更新したいとき

- **HTML を差し替える**: `app/src/main/assets/www/index.html` を新しいものに置き換えて push
- **バージョンを上げる**: `app/build.gradle` の `versionCode` / `versionName`
  （`versionCode` を増やしておくと、上書きインストールが素直に通ります）
- **アプリ名**: `app/src/main/res/values/strings.xml` の `app_name`
- **アイコン**: `app/src/main/res/drawable/ic_launcher_foreground.xml`

## 作りの要点

| 項目 | 内容 |
| --- | --- |
| 表示 | `WebViewAssetLoader` で `https://appassets.androidplatform.net/assets/www/index.html` として読み込む。`file://` ではないので `localStorage` と `navigator.clipboard` が制限なく使える |
| 保存 | `localStorage`（アプリのデータ領域）。端末のバックアップ対象 |
| 書き出し | WebView は `blob:` のダウンロードを扱えないため、`res/raw/download_hook.js` でクリックを横取りし、Android 側で Download フォルダへ書き出す |
| 読み込み | `WebChromeClient.onShowFileChooser` で端末のファイル選択を開く |
| 確認ダイアログ | `window.confirm` を `AlertDialog` で実装（未実装だと常に「いいえ」扱いになる） |
| 画面 | 全面表示にしたうえで、ステータスバー・ナビゲーションバー・ソフトキーボードの分だけ WebView を内側に寄せる |
| 最低 OS | Android 8.0（API 26） |

## 署名鍵について

`keystore/app.keystore` をリポジトリに入れてあります（パスワードは `android`）。
**秘密の鍵ではありません。** 毎回同じ署名にして「上書きインストールできる」ようにするためだけのものです。
Google Play に出す場合は、この鍵は使わずに配布用の鍵を作り直してください。

## 手元でビルドする場合

Android Studio（または Android SDK + JDK 17）があれば:

```bash
cd android
./gradlew assembleRelease
# app/build/outputs/apk/release/app-release.apk
```

## 既知の制限

- 見出しフォント（Zen Kaku Gothic New / Space Mono）は Google Fonts から読み込んでいます。
  オフラインでは端末内蔵のゴシック体になりますが、動作には影響しません。
- HTML が `color-mix(in oklab, ...)` と `100dvh` を使っているため、**Android System WebView**
  が Chrome 111 相当より古いと配色やレイアウトが崩れます。Play ストアで
  「Android System WebView」を更新しておいてください（Chrome で同じ HTML が正しく表示できていれば大丈夫）。
- 端末の「文字サイズ」設定を大きくしていると、レイアウトが窮屈になることがあります。
  HTML と同じ見た目に固定したい場合は `MainActivity.java` の WebView 設定に
  `settings.setTextZoom(100);` を足してください。
