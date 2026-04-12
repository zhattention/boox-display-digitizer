package com.boox.bridge

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.SurfaceHolder
import android.view.View
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import com.boox.bridge.databinding.ActivityMainBinding
import com.onyx.android.sdk.pen.RawInputCallback
import com.onyx.android.sdk.pen.TouchHelper
import com.onyx.android.sdk.data.note.TouchPoint
import com.onyx.android.sdk.pen.data.TouchPointList

/**
 * Captures raw pen input from the Boox stylus via Onyx SDK and streams it
 * to the Mac server.
 *
 * In Phase 1 we only capture and send. The SurfaceView is also used by the
 * Onyx SDK to render low-latency ink feedback (SDK's built-in renderer), so
 * the user sees what they wrote without us doing any drawing ourselves.
 *
 * The top-left overlay (@+id/overlay) for connection controls is registered
 * as an exclude rect so pen strokes over it are ignored.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val PREFS = "boox-bridge"
        private const val KEY_SERVER_URL = "server_url"
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var penSocket: PenSocket
    private var touchHelper: TouchHelper? = null

    // Boox screen size, in pixels. Used to normalize pen coordinates to 0..1
    // before sending to the server. We read this once from the SurfaceView
    // after it's laid out, so it's robust to any device (not just Tab X).
    private var boxW: Float = 1f
    private var boxH: Float = 1f

    // Whether the pen is currently touching the surface — used to avoid
    // repainting the background mid-stroke, which would erase live ink.
    @Volatile private var penDown: Boolean = false
    private val mainHandler = Handler(Looper.getMainLooper())
    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        penSocket = PenSocket(object : PenSocket.StateListener {
            override fun onStateChanged(state: PenSocket.State, message: String?) {
                updateStatusUi(state, message)
            }
            override fun onScreenshot(jpegBytes: ByteArray) {
                // We're on an OkHttp background thread here — decode, then
                // hand the bitmap off to the main thread for drawing.
                val bmp = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
                if (bmp == null) {
                    Log.w(TAG, "screenshot decode failed")
                    return
                }
                mainHandler.post { applyScreenshot(bmp) }
            }
        })

        // Restore last used server URL.
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

        // Initialize TouchHelper once we have real surface dimensions.
        // surfaceCreated may fire before layout, so width/height could still
        // be 0 there — we do the setup in surfaceChanged instead.
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

    private fun initTouchHelper(w: Int, h: Int) {
        if (w == 0 || h == 0) return
        if (touchHelper != null) return   // already set up

        boxW = w.toFloat()
        boxH = h.toFloat()
        Log.i(TAG, "surface size: ${w}x${h}")

        val surface = binding.surfaceView
        val limit = Rect(0, 0, w, h)
        val exclude = computeExcludeRects()
        Log.i(TAG, "limit=$limit exclude=$exclude")

        // SurfaceView's backing buffer is black by default. Paint it white
        // before TouchHelper takes over — once raw drawing is enabled, Onyx
        // writes ink directly on top of this background.
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
        // Draw a white background twice — SurfaceView uses double buffering,
        // so one lockCanvas paints only one of the two buffers.
        repeat(2) {
            val canvas = holder.lockCanvas() ?: return
            canvas.drawColor(Color.WHITE)
            holder.unlockCanvasAndPost(canvas)
        }
    }

    /**
     * Returns a list of rectangles (in SurfaceView coordinates) where the
     * pen should NOT trigger raw drawing — currently just the overlay with
     * the connect button and server URL field.
     */
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

    private val rawInputCallback = object : RawInputCallback() {
        override fun onBeginRawDrawing(shortcut: Boolean, point: TouchPoint) {
            penDown = true
            sendPoint(PenSocket.PEN_DOWN, point)
        }
        override fun onRawDrawingTouchPointMoveReceived(point: TouchPoint) {
            sendPoint(PenSocket.PEN_MOVE, point)
        }
        override fun onRawDrawingTouchPointListReceived(list: TouchPointList?) {
            // Not needed in Phase 1 — individual move events carry the same data.
        }
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

    /**
     * Draw the received screenshot onto the SurfaceView, stretched to fill
     * the whole surface. Called on the main thread. Skipped while the pen
     * is touching the surface to avoid erasing in-progress ink.
     */
    private fun applyScreenshot(bmp: Bitmap) {
        if (penDown) {
            Log.i(TAG, "skipping refresh: pen is down")
            return
        }
        val holder = binding.surfaceView.holder
        val dst = Rect(0, 0, boxW.toInt(), boxH.toInt())
        val src = Rect(0, 0, bmp.width, bmp.height)

        // Double-buffer: draw twice so both framebuffers show the new content.
        repeat(2) {
            val canvas = holder.lockCanvas() ?: return
            canvas.drawColor(Color.WHITE)
            canvas.drawBitmap(bmp, src, dst, bitmapPaint)
            holder.unlockCanvasAndPost(canvas)
        }
    }

    private fun sendPoint(type: Byte, point: TouchPoint) {
        val nx = point.x / boxW
        val ny = point.y / boxH
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
        touchHelper?.closeRawDrawing()
        touchHelper = null
        penSocket.close()
        super.onDestroy()
    }
}
