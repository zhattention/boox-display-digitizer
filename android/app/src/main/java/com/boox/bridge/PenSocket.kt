package com.boox.bridge

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit

/**
 * Thin WebSocket client that streams pen events to the Mac server.
 *
 * Wire format (matches server.py):
 *   1 byte  type    0=down 1=move 2=up 3=eraser_down 4=eraser_move 5=eraser_up
 *   4 bytes x       float32 big-endian, normalized 0.0..1.0
 *   4 bytes y       float32 big-endian, normalized 0.0..1.0
 */
class PenSocket(private val listener: StateListener) {

    enum class State { DISCONNECTED, CONNECTING, CONNECTED, FAILED }

    interface StateListener {
        fun onStateChanged(state: State, message: String?)
        // Called on a background (OkHttp) thread. Implementers should decode
        // and hand off to the UI thread themselves.
        fun onScreenshot(jpegBytes: ByteArray)
    }

    companion object {
        const val PEN_DOWN: Byte = 0
        const val PEN_MOVE: Byte = 1
        const val PEN_UP: Byte = 2
        const val ERASER_DOWN: Byte = 3
        const val ERASER_MOVE: Byte = 4
        const val ERASER_UP: Byte = 5
        const val MSG_SCREENSHOT: Byte = 0x10
        private const val TAG = "PenSocket"
        private const val PACKET_SIZE = 9
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .pingInterval(10, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // no read timeout for long-lived socket
        .build()

    private var webSocket: WebSocket? = null

    // Preallocated buffer to avoid GC churn on the hot path.
    private val scratch = ByteArray(PACKET_SIZE)
    private val scratchBuf: ByteBuffer = ByteBuffer.wrap(scratch).order(ByteOrder.BIG_ENDIAN)

    fun connect(url: String) {
        close()
        notifyState(State.CONNECTING, url)

        val request = try {
            Request.Builder().url(url).build()
        } catch (e: IllegalArgumentException) {
            notifyState(State.FAILED, "invalid url: ${e.message}")
            return
        }

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "connected to $url")
                notifyState(State.CONNECTED, null)
            }

            override fun onMessage(ws: WebSocket, bytes: ByteString) {
                if (bytes.size < 1) return
                when (bytes[0]) {
                    MSG_SCREENSHOT -> {
                        val jpeg = bytes.substring(1).toByteArray()
                        Log.i(TAG, "screenshot frame: ${jpeg.size} bytes")
                        listener.onScreenshot(jpeg)
                    }
                    else -> Log.w(TAG, "unknown downstream type: ${bytes[0]}")
                }
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "failure: ${t.message}")
                notifyState(State.FAILED, t.message)
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "closed: $reason")
                notifyState(State.DISCONNECTED, null)
            }
        })
    }

    /**
     * Send a single-byte request asking the server to capture and send back
     * a screenshot of the target rectangle. No-op if not connected.
     */
    fun requestScreenshot() {
        val ws = webSocket ?: return
        ws.send(byteArrayOf(MSG_SCREENSHOT).toByteString(0, 1))
    }

    /**
     * Send one pen event. [normX] and [normY] must be normalized to [0, 1].
     * Safe to call from any thread — OkHttp serializes writes internally.
     */
    fun sendPen(type: Byte, normX: Float, normY: Float) {
        val ws = webSocket ?: return
        synchronized(scratch) {
            scratchBuf.clear()
            scratchBuf.put(type)
            scratchBuf.putFloat(normX)
            scratchBuf.putFloat(normY)
            ws.send(scratch.toByteString(0, PACKET_SIZE))
        }
    }

    fun close() {
        webSocket?.close(1000, "client closing")
        webSocket = null
    }

    private fun notifyState(state: State, message: String?) {
        mainHandler.post { listener.onStateChanged(state, message) }
    }
}
