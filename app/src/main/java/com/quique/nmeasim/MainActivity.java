package com.quique.nmeasim;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

public class MainActivity extends Activity {
    private static final String URL = "http://127.0.0.1:" + SimService.WEB_PORT + "/";
    private static final int MAX_RETRIES = 60;

    private WebView web;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private int retries = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Android 13+: sin este permiso la notificación del servicio no se muestra (el servicio funciona igual).
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        startForegroundService(new Intent(this, SimService.class));

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0b111e"));
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        // Con targetSdk 35 la app se dibuja bajo las barras del sistema: se compensa con padding.
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top = insets.getInsets(android.view.WindowInsets.Type.systemBars()).top;
            int bottom = Math.max(
                    insets.getInsets(android.view.WindowInsets.Type.systemBars()).bottom,
                    insets.getInsets(android.view.WindowInsets.Type.ime()).bottom);
            v.setPadding(0, top, 0, bottom);
            return android.view.WindowInsets.CONSUMED;
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // localStorage (vista móvil/escritorio)
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                // El servidor Python tarda un par de segundos en arrancar: se reintenta la página principal.
                if (req.isForMainFrame() && retries++ < MAX_RETRIES) {
                    handler.postDelayed(() -> web.loadUrl(URL), 700);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            // La UI usa alert()/confirm(); sin esto el WebView los ignora.
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
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
        });

        web.loadUrl(URL);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        // Atrás no cierra el simulador: lo manda a segundo plano (se detiene desde la notificación).
        moveTaskToBack(true);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
