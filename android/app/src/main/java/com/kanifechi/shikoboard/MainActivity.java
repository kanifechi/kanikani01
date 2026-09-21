package com.kanifechi.shikoboard;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.JsResult;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.annotation.RequiresApi;
import androidx.annotation.Nullable;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import androidx.webkit.WebViewAssetLoader;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 同梱した 1 枚の HTML（app/src/main/assets/www/index.html）を WebView で表示するだけのアプリ。
 *
 * file:// ではなく WebViewAssetLoader で https://appassets.androidplatform.net/ として読ませている。
 * こうすると「安全なオリジン」扱いになるので、localStorage が確実に永続化され、
 * navigator.clipboard も使える（file:// だとどちらも制限される）。
 */
public class MainActivity extends Activity {

    private static final String APP_HOST = "appassets.androidplatform.net";
    private static final String START_URL = "https://" + APP_HOST + "/assets/www/index.html";

    private static final int REQ_PICK_FILE = 1001;
    private static final int REQ_WRITE_STORAGE = 1002;

    private WebView webView;
    private WebViewAssetLoader assetLoader;
    private ValueCallback<Uri[]> pendingFileCallback;
    private PendingSave pendingSave;
    private String downloadHookJs;

    /** 権限ダイアログをはさむ場合があるので、書き出し内容を一旦持っておく入れ物。 */
    private static final class PendingSave {
        final String name;
        final String mime;
        final byte[] bytes;

        PendingSave(String name, String mime, byte[] bytes) {
            this.name = name;
            this.mime = mime;
            this.bytes = bytes;
        }
    }

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        downloadHookJs = readRawText(R.raw.download_hook);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(getColor(R.color.paper));

        webView = new WebView(this);
        webView.setBackgroundColor(getColor(R.color.paper));
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        setUpWindow(root);
        setUpWebView();

        // restoreState() は WebView の状態が入っていない Bundle だと null を返す。
        // そのまま放置すると真っ白な画面になるので、必ず読み込みにフォールバックする。
        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            webView.loadUrl(START_URL);
        }
    }

    /**
     * 画面いっぱいに描いたうえで、ステータスバー・ナビゲーションバー・ソフトキーボードの分だけ
     * WebView を内側に寄せる。HTML 側は 100dvh でレイアウトしているので、高さを削るだけでよい。
     */
    private void setUpWindow(View root) {
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(false);
        }

        WindowInsetsControllerCompat bars =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        bars.setAppearanceLightStatusBars(true);
        bars.setAppearanceLightNavigationBars(true);

        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            Insets system = insets.getInsets(
                    WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            view.setPadding(system.left, system.top, system.right,
                    Math.max(system.bottom, ime.bottom));
            return WindowInsetsCompat.CONSUMED;
        });
    }

    private void setUpWebView() {
        assetLoader = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);   // localStorage = このアプリのデータ保存先
        settings.setDatabaseEnabled(true);
        settings.setUseWideViewPort(true);     // viewport メタタグをブラウザと同じように解釈させる
        settings.setLoadWithOverviewMode(false);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setAllowFileAccess(false);    // 表示は AssetLoader 経由なので file:// は塞いでよい
        settings.setAllowContentAccess(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);

        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        webView.addJavascriptInterface(new FileSaverBridge(), "AndroidFileSaver");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return assetLoader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if (APP_HOST.equals(url.getHost())) {
                    return false;
                }
                openExternally(url);
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (downloadHookJs != null) {
                    view.evaluateJavascript(downloadHookJs, null);
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setCancelable(false)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setNegativeButton(android.R.string.cancel, (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (pendingFileCallback != null) {
                    pendingFileCallback.onReceiveValue(null);
                }
                pendingFileCallback = callback;

                // params.createIntent() だと .md / .markdown のような拡張子指定を拾えない端末があるので
                // 自前で「すべてのファイル」を出す。
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                try {
                    startActivityForResult(intent, REQ_PICK_FILE);
                    return true;
                } catch (ActivityNotFoundException e) {
                    pendingFileCallback = null;
                    toast(getString(R.string.no_file_picker));
                    return false;
                }
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, @Nullable Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_FILE) {
            return;
        }
        Uri[] result = null;
        if (resultCode == RESULT_OK && data != null) {
            result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        }
        // キャンセル時も必ず返さないと <input type="file"> が二度と反応しなくなる
        if (pendingFileCallback != null) {
            pendingFileCallback.onReceiveValue(result);
            pendingFileCallback = null;
        }
    }

    /** JS からの書き出し要求を受ける窓口。 */
    private final class FileSaverBridge {
        @JavascriptInterface
        public void save(String name, String mime, String base64) {
            final byte[] bytes;
            try {
                bytes = Base64.decode(base64, Base64.DEFAULT);
            } catch (IllegalArgumentException e) {
                runOnUiThread(() -> toast(getString(R.string.save_failed)));
                return;
            }
            runOnUiThread(() -> saveToDownloads(new PendingSave(name, mime, bytes)));
        }

        @JavascriptInterface
        public void failed(String reason) {
            runOnUiThread(() -> toast(getString(R.string.save_failed)));
        }
    }

    private void saveToDownloads(PendingSave save) {
        String name = sanitizeFileName(save.name);
        String mime = (save.mime == null || save.mime.isEmpty())
                ? "application/octet-stream" : save.mime;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                writeViaMediaStore(name, mime, save.bytes);
            } else {
                if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                        != PackageManager.PERMISSION_GRANTED) {
                    pendingSave = save;
                    requestPermissions(
                            new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE},
                            REQ_WRITE_STORAGE);
                    return;
                }
                File dir = Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_DOWNLOADS);
                if (!dir.exists() && !dir.mkdirs()) {
                    throw new IOException("mkdirs failed");
                }
                File target = uniqueFile(dir, name);
                try (FileOutputStream out = new FileOutputStream(target)) {
                    out.write(save.bytes);
                }
                MediaScannerConnection.scanFile(this,
                        new String[]{target.getAbsolutePath()}, new String[]{mime}, null);
                name = target.getName();
            }
            toast(getString(R.string.saved_to_downloads, name));
        } catch (Exception e) {
            toast(getString(R.string.save_failed));
        }
    }

    /** Android 10 以降。権限なしで Download フォルダへ書ける。 */
    @RequiresApi(Build.VERSION_CODES.Q)
    private void writeViaMediaStore(String name, String mime, byte[] bytes) throws IOException {
        ContentValues values = new ContentValues();
        values.put(MediaStore.Downloads.DISPLAY_NAME, name);
        values.put(MediaStore.Downloads.MIME_TYPE, mime);
        values.put(MediaStore.Downloads.IS_PENDING, 1);

        Uri item = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (item == null) {
            throw new IOException("MediaStore insert failed");
        }
        try (OutputStream out = getContentResolver().openOutputStream(item)) {
            if (out == null) {
                throw new IOException("openOutputStream failed");
            }
            out.write(bytes);
        }
        values.clear();
        values.put(MediaStore.Downloads.IS_PENDING, 0);
        getContentResolver().update(item, values, null, null);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_WRITE_STORAGE) {
            return;
        }
        PendingSave save = pendingSave;
        pendingSave = null;
        if (save == null) {
            return;
        }
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            saveToDownloads(save);
        } else {
            toast(getString(R.string.need_storage_permission));
        }
    }

    private static String sanitizeFileName(String raw) {
        String name = (raw == null || raw.trim().isEmpty()) ? "download" : raw.trim();
        name = name.replaceAll("[\\\\/:*?\"<>|\\x00-\\x1f]", "_");
        if (name.length() > 120) {
            name = name.substring(0, 120);
        }
        return name;
    }

    private static File uniqueFile(File dir, String name) {
        File file = new File(dir, name);
        if (!file.exists()) {
            return file;
        }
        int dot = name.lastIndexOf('.');
        String base = dot > 0 ? name.substring(0, dot) : name;
        String ext = dot > 0 ? name.substring(dot) : "";
        for (int i = 1; i < 1000; i++) {
            file = new File(dir, base + "(" + i + ")" + ext);
            if (!file.exists()) {
                return file;
            }
        }
        return new File(dir, base + "-" + System.currentTimeMillis() + ext);
    }

    private void openExternally(Uri url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, url)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (ActivityNotFoundException ignored) {
            // 開けるアプリがなければ何もしない
        }
    }

    @Nullable
    private String readRawText(int resId) {
        try (InputStream in = getResources().openRawResource(resId);
             ByteArrayOutputStream buffer = new ByteArrayOutputStream()) {
            byte[] chunk = new byte[4096];
            int read;
            while ((read = in.read(chunk)) > 0) {
                buffer.write(chunk, 0, read);
            }
            return buffer.toString("UTF-8");
        } catch (IOException e) {
            return null;
        }
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onSaveInstanceState(@NonNull Bundle outState) {
        super.onSaveInstanceState(outState);
        webView.saveState(outState);
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
    }

    @Override
    protected void onPause() {
        webView.onPause();
        super.onPause();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (pendingFileCallback != null) {
            pendingFileCallback.onReceiveValue(null);
            pendingFileCallback = null;
        }
        webView.destroy();
        super.onDestroy();
    }
}
