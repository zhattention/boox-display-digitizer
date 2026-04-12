package com.boox.bridge

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.SurfaceHolder
import android.view.View
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import com.boox.bridge.databinding.ActivityMainBinding
import com.onyx.android.sdk.pen.RawInputCallback
import com.onyx.android.sdk.pen.TouchHelper
import com.onyx.android.sdk.data.note.TouchPoint
import com.onyx.android.sdk.pen.data.TouchPointList

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val PREFS = "boox-bridge"
        private const val KEY_SERVER_URL = "server_url"
        private const val MIN_ZOOM = 0.5f
        private const val MAX_ZOOM = 5.0f
        private const val DISCOVERY_PORT = 9998
        private val DISCOVERY_MAGIC = "BOOX-BRIDGE".toByteArray()
        private const val NSD_SERVICE_TYPE = "_boox-bridge._tcp."
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var penSocket: PenSocket
    private var touchHelper: TouchHelper? = null

    private var surfW: Float = 1f
    private var surfH: Float = 1f

    private var macW: Float = 0f
    private var macH: Float = 0f

    private var baseScale: Float = 1f
    private var userZoom: Float = 1f
    private var panX: Float = 0f
    private var panY: Float = 0f

    @Volatile private var penDown: Boolean = false
    private val mainHandler = Handler(Looper.getMainLooper())
    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)

    private var lastScreenshot: Bitmap? = null

    private lateinit var scaleDetector: ScaleGestureDetector
    private var twoFingerDragging: Boolean = false
    private var lastMidX: Float = 0f
    private var lastMidY: Float = 0f

    // Discovery: NSD (mDNS) + UDP broadcast fallback, first one wins
    private var nsdManager: NsdManager? = null
    private var nsdActive: Boolean = false
    private var multicastLock: WifiManager.MulticastLock? = null
    @Volatile private var udpDiscoveryRunning: Boolean = false
    private var udpDiscoveryThread: Thread? = null
    @Volatile private var serverDiscovered: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableHiddenApi()
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        scaleDetector = ScaleGestureDetector(this, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                if (penDown) return false
                val focusX = detector.focusX
                val focusY = detector.focusY
                val oldZoom = userZoom
                userZoom = (userZoom * detector.scaleFactor).coerceIn(MIN_ZOOM, MAX_ZOOM)
                val zoomDelta = userZoom / oldZoom
                panX = focusX - (focusX - panX) * zoomDelta
                panY = focusY - (focusY - panY) * zoomDelta
                clampPan()
                redrawSurface()
                return true
            }
        })

        penSocket = PenSocket(object : PenSocket.StateListener {
            override fun onStateChanged(state: PenSocket.State, message: String?) {
                updateStatusUi(state, message)
            }
            override fun onScreenshot(jpegBytes: ByteArray) {
                val bmp = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
                if (bmp == null) {
                    Log.w(TAG, "screenshot decode failed")
                    return
                }
                mainHandler.post {
                    lastScreenshot = bmp
                    redrawSurface()
                }
            }
            override fun onScreenInfo(width: Int, height: Int) {
                macW = width.toFloat()
                macH = height.toFloat()
                Log.i(TAG, "Mac screen: ${width}x${height} points")
                recalcViewport()
            }
        })

        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        binding.serverUrlInput.setText(prefs.getString(KEY_SERVER_URL, "ws://192.168.1.100:9999"))

        binding.connectButton.setOnClickListener {
            val url = binding.serverUrlInput.text.toString().trim()
            prefs.edit().putString(KEY_SERVER_URL, url).apply()
            penSocket.connect(url)
        }
        binding.serverUrlInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                binding.connectButton.performClick()
                true
            } else false
        }
        binding.refreshButton.setOnClickListener {
            penSocket.requestScreenshot()
        }

        // Auto-discover server: NSD (mDNS) + UDP broadcast in parallel.
        startDiscovery()

        binding.surfaceView.holder.addCallback(object : SurfaceHolder.Callback {
            override fun surfaceCreated(holder: SurfaceHolder) {}
            override fun surfaceChanged(holder: SurfaceHolder, format: Int, w: Int, h: Int) {
                initTouchHelper(w, h)
            }
            override fun surfaceDestroyed(holder: SurfaceHolder) {
                touchHelper?.setRawDrawingEnabled(false)
            }
        })
    }

    /**
     * Intercept all touch events at the Activity level.
     * - If touch lands on the overlay (buttons), let it through normally.
     * - Otherwise, only process 2-finger gestures; ignore single-finger (palm).
     */
    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        // Always let the overlay (buttons) handle its own touches.
        if (isTouchOnOverlay(ev)) {
            return super.dispatchTouchEvent(ev)
        }

        // Feed all events to ScaleGestureDetector (it only acts on 2+ pointers internally).
        scaleDetector.onTouchEvent(ev)

        // Manual 2-finger pan tracking.
        if (!penDown) {
            handleTwoFingerPan(ev)
        }

        // Don't pass single-finger events down to child views (prevents
        // SurfaceView / other views from reacting to palm touches).
        // 2-finger events are already handled above.
        if (ev.pointerCount >= 2) {
            return true
        }

        return super.dispatchTouchEvent(ev)
    }

    private fun isTouchOnOverlay(ev: MotionEvent): Boolean {
        val overlay = binding.overlay
        if (overlay.visibility != View.VISIBLE) return false
        val loc = IntArray(2).also { overlay.getLocationOnScreen(it) }
        val x = ev.rawX
        val y = ev.rawY
        return x >= loc[0] && x <= loc[0] + overlay.width &&
               y >= loc[1] && y <= loc[1] + overlay.height
    }

    private fun handleTwoFingerPan(ev: MotionEvent) {
        when (ev.actionMasked) {
            MotionEvent.ACTION_POINTER_DOWN -> {
                if (ev.pointerCount == 2) {
                    twoFingerDragging = true
                    lastMidX = (ev.getX(0) + ev.getX(1)) / 2f
                    lastMidY = (ev.getY(0) + ev.getY(1)) / 2f
                }
            }
            MotionEvent.ACTION_MOVE -> {
                if (twoFingerDragging && ev.pointerCount >= 2) {
                    val midX = (ev.getX(0) + ev.getX(1)) / 2f
                    val midY = (ev.getY(0) + ev.getY(1)) / 2f
                    panX += midX - lastMidX
                    panY += midY - lastMidY
                    lastMidX = midX
                    lastMidY = midY
                    clampPan()
                    redrawSurface()
                }
            }
            MotionEvent.ACTION_POINTER_UP, MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (ev.pointerCount <= 2) {
                    twoFingerDragging = false
                }
            }
        }
    }

    private fun recalcViewport() {
        if (macW <= 0 || macH <= 0 || surfW <= 0 || surfH <= 0) return
        baseScale = minOf(surfW / macW, surfH / macH)
        userZoom = 1f
        val imgW = macW * baseScale
        val imgH = macH * baseScale
        panX = (surfW - imgW) / 2f
        panY = (surfH - imgH) / 2f
        Log.i(TAG, "viewport: baseScale=$baseScale panX=$panX panY=$panY")
        redrawSurface()
    }

    private fun clampPan() {
        val scale = baseScale * userZoom
        val imgW = macW * scale
        val imgH = macH * scale
        val minV = 0.2f
        val loX = minOf(imgW, surfW) * minV - imgW
        val hiX = surfW - minOf(imgW, surfW) * minV
        panX = if (loX < hiX) panX.coerceIn(loX, hiX) else (surfW - imgW) / 2f
        val loY = minOf(imgH, surfH) * minV - imgH
        val hiY = surfH - minOf(imgH, surfH) * minV
        panY = if (loY < hiY) panY.coerceIn(loY, hiY) else (surfH - imgH) / 2f
    }

    private fun initTouchHelper(w: Int, h: Int) {
        if (w == 0 || h == 0) return
        if (touchHelper != null) return

        surfW = w.toFloat()
        surfH = h.toFloat()
        Log.i(TAG, "surface size: ${w}x${h}")

        if (macW > 0 && macH > 0) recalcViewport()

        val surface = binding.surfaceView
        val limit = Rect(0, 0, w, h)
        val exclude = computeExcludeRects()

        paintSurfaceWhite(surface.holder)

        touchHelper = TouchHelper.create(surface, rawInputCallback)
            .setStrokeStyle(TouchHelper.STROKE_STYLE_PENCIL)
            .setStrokeColor(Color.BLACK)
            .setStrokeWidth(3.0f)
            .setLimitRect(limit, exclude)
            .openRawDrawing()
            .also { it.setRawDrawingEnabled(true) }
    }

    private fun paintSurfaceWhite(holder: SurfaceHolder) {
        repeat(2) {
            val canvas = holder.lockCanvas() ?: return
            canvas.drawColor(Color.WHITE)
            holder.unlockCanvasAndPost(canvas)
        }
    }

    private fun redrawSurface() {
        if (penDown) return
        val bmp = lastScreenshot ?: return
        if (macW <= 0 || macH <= 0) return

        // Onyx SDK locks the SurfaceView canvas during raw drawing.
        // Temporarily disable to allow our background update.
        val th = touchHelper
        th?.setRawDrawingEnabled(false)

        val holder = binding.surfaceView.holder
        val scale = baseScale * userZoom
        val dstW = (macW * scale).toInt()
        val dstH = (macH * scale).toInt()
        val dst = Rect(panX.toInt(), panY.toInt(), panX.toInt() + dstW, panY.toInt() + dstH)
        val src = Rect(0, 0, bmp.width, bmp.height)

        repeat(2) {
            val canvas = holder.lockCanvas() ?: return@repeat
            canvas.drawColor(Color.WHITE)
            canvas.drawBitmap(bmp, src, dst, bitmapPaint)
            holder.unlockCanvasAndPost(canvas)
        }

        th?.setRawDrawingEnabled(true)
    }

    private fun computeExcludeRects(): ArrayList<Rect> {
        val overlay = binding.overlay
        val result = ArrayList<Rect>()
        if (overlay.visibility != View.VISIBLE) return result
        val surfaceLoc = IntArray(2).also { binding.surfaceView.getLocationOnScreen(it) }
        val overlayLoc = IntArray(2).also { overlay.getLocationOnScreen(it) }
        val left = overlayLoc[0] - surfaceLoc[0]
        val top = overlayLoc[1] - surfaceLoc[1]
        result.add(Rect(left, top, left + overlay.width, top + overlay.height))
        return result
    }

    private fun mapToMac(surfX: Float, surfY: Float): Pair<Float, Float> {
        if (macW <= 0 || macH <= 0) return Pair(surfX / surfW, surfY / surfH)
        val scale = baseScale * userZoom
        val macX = (surfX - panX) / scale
        val macY = (surfY - panY) / scale
        return Pair(macX / macW, macY / macH)
    }

    private val rawInputCallback = object : RawInputCallback() {
        override fun onBeginRawDrawing(shortcut: Boolean, point: TouchPoint) {
            penDown = true
            sendPoint(PenSocket.PEN_DOWN, point)
        }
        override fun onRawDrawingTouchPointMoveReceived(point: TouchPoint) {
            sendPoint(PenSocket.PEN_MOVE, point)
        }
        override fun onRawDrawingTouchPointListReceived(list: TouchPointList?) {}
        override fun onEndRawDrawing(outOfRange: Boolean, point: TouchPoint) {
            sendPoint(PenSocket.PEN_UP, point)
            penDown = false
        }
        override fun onBeginRawErasing(shortcut: Boolean, point: TouchPoint) {
            penDown = true
            sendPoint(PenSocket.ERASER_DOWN, point)
        }
        override fun onRawErasingTouchPointMoveReceived(point: TouchPoint) {
            sendPoint(PenSocket.ERASER_MOVE, point)
        }
        override fun onRawErasingTouchPointListReceived(list: TouchPointList?) {}
        override fun onEndRawErasing(outOfRange: Boolean, point: TouchPoint) {
            sendPoint(PenSocket.ERASER_UP, point)
            penDown = false
        }
    }

    private fun sendPoint(type: Byte, point: TouchPoint) {
        val (nx, ny) = mapToMac(point.x, point.y)
        penSocket.sendPen(type, nx, ny)
    }

    private fun updateStatusUi(state: PenSocket.State, message: String?) {
        val text = when (state) {
            PenSocket.State.DISCONNECTED -> getString(R.string.status_disconnected)
            PenSocket.State.CONNECTING -> getString(R.string.status_connecting)
            PenSocket.State.CONNECTED -> getString(R.string.status_connected)
            PenSocket.State.FAILED ->
                message?.let { "${getString(R.string.status_failed)}: $it" }
                    ?: getString(R.string.status_failed)
        }
        binding.statusText.text = text
    }

    override fun onResume() {
        super.onResume()
        touchHelper?.setRawDrawingEnabled(true)
    }

    override fun onPause() {
        touchHelper?.setRawDrawingEnabled(false)
        super.onPause()
    }

    override fun onDestroy() {
        stopDiscovery()
        touchHelper?.closeRawDrawing()
        touchHelper = null
        penSocket.close()
        super.onDestroy()
    }

    private fun enableHiddenApi() {
        try {
            Settings.Global.putInt(contentResolver, "hidden_api_policy", 1)
            Log.i(TAG, "hidden_api_policy set to 1")
        } catch (e: SecurityException) {
            Log.w(TAG, "Cannot set hidden_api_policy (grant via: adb shell pm grant com.boox.bridge android.permission.WRITE_SECURE_SETTINGS)")
        }
    }

    // ---- Server discovery (NSD + UDP broadcast) ----------------------------

    private fun onServerFound(host: String, port: Int, source: String) {
        if (serverDiscovered) return
        serverDiscovered = true
        val url = "ws://$host:$port"
        Log.i(TAG, "Server found via $source: $url")
        mainHandler.post {
            binding.serverUrlInput.setText(url)
            binding.statusText.text = "found: $host"
        }
    }

    private fun startDiscovery() {
        // 1) NSD (mDNS / Bonjour)
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        multicastLock = wifi.createMulticastLock("boox-bridge").apply {
            setReferenceCounted(false)
            acquire()
        }
        nsdManager = (getSystemService(Context.NSD_SERVICE) as NsdManager).also {
            it.discoverServices(NSD_SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, nsdDiscoveryListener)
            nsdActive = true
        }

        // 2) UDP broadcast fallback
        udpDiscoveryRunning = true
        udpDiscoveryThread = Thread({
            try {
                val sock = DatagramSocket(DISCOVERY_PORT)
                sock.broadcast = true
                sock.soTimeout = 5000
                val buf = ByteArray(64)
                val pkt = DatagramPacket(buf, buf.size)
                Log.i(TAG, "UDP discovery listening on port $DISCOVERY_PORT")
                while (udpDiscoveryRunning && !serverDiscovered) {
                    try {
                        sock.receive(pkt)
                        val magic = DISCOVERY_MAGIC
                        if (pkt.length >= magic.size + 2) {
                            val data = pkt.data
                            val match = (0 until magic.size).all { data[pkt.offset + it] == magic[it] }
                            if (match) {
                                val portBuf = ByteBuffer.wrap(data, pkt.offset + magic.size, 2)
                                    .order(ByteOrder.BIG_ENDIAN)
                                val wsPort = portBuf.short.toInt() and 0xFFFF
                                val host = pkt.address.hostAddress ?: continue
                                onServerFound(host, wsPort, "UDP")
                            }
                        }
                    } catch (_: java.net.SocketTimeoutException) {
                        // retry
                    }
                }
                sock.close()
            } catch (e: Exception) {
                Log.w(TAG, "UDP discovery error: ${e.message}")
            }
        }, "udp-discovery").also { it.isDaemon = true; it.start() }
    }

    private val nsdDiscoveryListener = object : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(serviceType: String) {
            Log.i(TAG, "NSD discovery started")
        }
        override fun onServiceFound(info: NsdServiceInfo) {
            Log.i(TAG, "NSD service found: ${info.serviceName}")
            nsdManager?.resolveService(info, nsdResolveListener)
        }
        override fun onServiceLost(info: NsdServiceInfo) {}
        override fun onDiscoveryStopped(serviceType: String) { nsdActive = false }
        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
            Log.w(TAG, "NSD start failed: $errorCode")
            nsdActive = false
        }
        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
    }

    private val nsdResolveListener = object : NsdManager.ResolveListener {
        override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
            Log.w(TAG, "NSD resolve failed: $errorCode")
        }
        override fun onServiceResolved(info: NsdServiceInfo) {
            val host = info.host?.hostAddress ?: return
            onServerFound(host, info.port, "NSD")
        }
    }

    private fun stopDiscovery() {
        udpDiscoveryRunning = false
        udpDiscoveryThread?.interrupt()
        if (nsdActive) {
            try { nsdManager?.stopServiceDiscovery(nsdDiscoveryListener) } catch (_: Exception) {}
        }
        multicastLock?.let { if (it.isHeld) it.release() }
    }
}
