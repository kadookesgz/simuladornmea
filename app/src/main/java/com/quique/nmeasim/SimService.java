package com.quique.nmeasim;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

/** Ejecuta el simulador Python en primer plano para que siga emitiendo con la pantalla apagada. */
public class SimService extends Service {
    static final String ACTION_STOP = "com.quique.nmeasim.STOP";
    static final int TCP_PORT = 10111;
    static final int WEB_PORT = 8085;

    private static final String CHANNEL_ID = "nmea_sim";
    private static final String TAG = "NmeaSim";

    private PowerManager.WakeLock wakeLock;
    private boolean pythonStarted = false;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopForeground(true);
            stopSelf();
            android.os.Process.killProcess(android.os.Process.myPid());
            return START_NOT_STICKY;
        }

        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(
                CHANNEL_ID, "Simulador NMEA", NotificationManager.IMPORTANCE_LOW));

        Intent open = new Intent(this, MainActivity.class);
        PendingIntent openPi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, SimService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);

        Notification n = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Simulador NMEA en marcha")
                .setContentText("TCP " + TCP_PORT + " · Web " + WEB_PORT)
                .setContentIntent(openPi)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Detener", stopPi).build())
                .build();

        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, n);
        }

        if (wakeLock == null) {
            PowerManager pm = getSystemService(PowerManager.class);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "NmeaSim:sim");
            wakeLock.acquire();
        }

        if (!pythonStarted) {
            pythonStarted = true;
            new Thread(() -> {
                try {
                    PyObject mod = Python.getInstance().getModule("simulador_nmea_ais_arpa_profundidad");
                    // Web solo en 127.0.0.1: los controles no quedan expuestos a la red.
                    PyObject r = mod.callAttr("start_background", TCP_PORT, WEB_PORT, "127.0.0.1");
                    Log.i(TAG, "Simulador iniciado: " + r);
                } catch (Throwable t) {
                    Log.e(TAG, "No se pudo iniciar el simulador", t);
                    pythonStarted = false;
                }
            }, "sim-start").start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
