package com.hanryxvault.pos

import android.os.*
import android.content.*
import android.net.Uri
import android.util.Log
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.*
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import coil.compose.AsyncImage
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import java.text.SimpleDateFormat
import java.util.Date
import androidx.lifecycle.lifecycleScope
import com.zettle.sdk.ZettleSDK
import com.zettle.sdk.feature.cardreader.ui.CardReaderAction
import com.zettle.sdk.feature.cardreader.payment.TransactionReference
import com.zettle.sdk.feature.cardreader.payment.TippingConfiguration
import android.bluetooth.BluetoothManager
import com.zettle.sdk.ui.ZettleResult
import com.zettle.sdk.ui.zettleResult
import com.zettle.sdk.ui.ZettleActivity
import com.zettle.sdk.feature.qrc.QrcAction
import com.zettle.sdk.feature.qrc.venmo.VenmoQrcAction
import kotlinx.coroutines.*
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import java.util.UUID
import java.util.Currency
import android.app.Activity.RESULT_OK
import android.content.pm.PackageManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.Observer
import com.zettle.sdk.core.auth.User
import android.media.AudioManager
import android.media.ToneGenerator
import android.graphics.Bitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.window.Dialog
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.res.painterResource
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.imePadding
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.input.KeyboardCapitalization
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import java.io.ByteArrayOutputStream
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.zIndex

// ⚜️ VAULT ELITE BRANDING
val Gold = Color(0xFFC5A059)
val DeepGold = Color(0xFF8E6E37)
val VaultBlack = Color(0xFF0A0A0A)
val VaultGrey = Color(0xFF161616)
val VaultSurface = Color(0xFF222222)

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    private val viewModel: MainViewModel by viewModels()
    
    @Inject lateinit var paymentManager: PaymentManager
    @Inject lateinit var scannerManager: ScannerManager

    private var barcodeBuffer = StringBuilder()
    private lateinit var feedbackManager: FeedbackManager

    // LiveData observer on ZettleSDK.authState — keeps isZettleAuthenticated indicator current.
    private var zettleAuthObserver: Observer<User.AuthState>? = null

    companion object {
        const val ZETTLE_LOGIN_REQUEST = 0x5A1F
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.data?.scheme == "hanryxvaultdone") {
            viewModel.markZettleServerConnected()  // turns icon green immediately
            viewModel.executePendingZettlePayment()
        }
        // Widget "PAY" / "OPEN APP" button — activate trade-in mode and pre-load widget credit
        if (intent.getBooleanExtra("OPEN_TRADE_IN", false)) {
            viewModel.handleIntent(POSIntent.ActivateTradeIn)
            val widgetCredit = intent.getFloatExtra("WIDGET_TRADE_CREDIT", 0f)
            if (widgetCredit > 0f) {
                viewModel.handleIntent(POSIntent.SetWidgetTradeCredit(widgetCredit))
            }
        }
    }

    @Suppress("OVERRIDE_DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == ZETTLE_LOGIN_REQUEST) {
            Log.d("VaultAuth", "Zettle login activity returned: resultCode=$resultCode")
            // The SDK completes token exchange asynchronously; poll a few times to catch it.
            lifecycleScope.launch {
                repeat(5) { attempt ->
                    delay(500L * (attempt + 1))
                    viewModel.updateSdkLoginState()
                    if (com.zettle.sdk.ZettleSDK.instance?.isLoggedIn == true) {
                        Log.d("VaultAuth", "SDK login confirmed on attempt ${attempt + 1}")
                        Toast.makeText(this@MainActivity, "Zettle account connected!", Toast.LENGTH_SHORT).show()
                        return@launch
                    }
                }
                // Final check after all retries
                if (com.zettle.sdk.ZettleSDK.instance?.isLoggedIn != true) {
                    Toast.makeText(this@MainActivity, "Zettle login incomplete — try again.", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN) {
            val char = event.unicodeChar.toChar()
            if (event.keyCode == KeyEvent.KEYCODE_ENTER) {
                val code = barcodeBuffer.toString().trim()
                if (code.isNotEmpty()) {
                    scannerManager.onDataReceived(code)
                    barcodeBuffer.setLength(0)
                    feedbackManager.vibrate(50)
                    feedbackManager.playSuccessSound()
                }
                return true
            } else if (char.isLetterOrDigit() || char == '-' || char == '_') {
                barcodeBuffer.append(char)
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        feedbackManager = FeedbackManager(this)

        // ✅ INIT ONCE ONLY
        ZettleInitializer.init(
            applicationContext, 
            BuildConfig.ZETTLE_CLIENT_ID, 
            BuildConfig.ZETTLE_REDIRECT_URL
        )
        // Observe Zettle auth state via LiveData — fires reliably when OAuth completes.
        setupZettleAuthObserver()
        // Request Bluetooth + Location permissions so Zettle BLE scanner can find the card reader.
        requestBluetoothPermissions()

        // If launched from the Trade Calc widget's "PAY" button, activate trade-in and load widget credit
        if (intent.getBooleanExtra("OPEN_TRADE_IN", false)) {
            viewModel.handleIntent(POSIntent.ActivateTradeIn)
            val widgetCredit = intent.getFloatExtra("WIDGET_TRADE_CREDIT", 0f)
            if (widgetCredit > 0f) {
                viewModel.handleIntent(POSIntent.SetWidgetTradeCredit(widgetCredit))
            }
        }

        setContent {
            val state by viewModel.state.collectAsState()

            // 🔒 REGISTER MODE — block back press and hide system bars
            BackHandler(enabled = state.isRegisterMode) { /* swallow back presses */ }

            LaunchedEffect(state.isRegisterMode) {
                val controller = WindowInsetsControllerCompat(window, window.decorView)
                if (state.isRegisterMode) {
                    window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    WindowCompat.setDecorFitsSystemWindows(window, false)
                    controller.hide(WindowInsetsCompat.Type.systemBars())
                    controller.systemBarsBehavior =
                        WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                    try { startLockTask() } catch (_: Exception) {}
                } else {
                    window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    WindowCompat.setDecorFitsSystemWindows(window, true)
                    controller.show(WindowInsetsCompat.Type.systemBars())
                    try { stopLockTask() } catch (_: Exception) {}
                }
            }

            VaultTheme {
                Box(Modifier.fillMaxSize().background(VaultBlack)) {
                    Watermark()
                    
                    Crossfade(targetState = state.isLoading, label = "Loading") { isLoading ->
                        if (isLoading) {
                            LoadingScreen()
                        } else {
                            Crossfade(targetState = state.employeeId == null, label = "Auth") { isLoggedOut ->
                                if (isLoggedOut) {
                                    LoginScreen { id -> viewModel.handleIntent(POSIntent.Authorize(id)) }
                                } else {
                                    MainPOSLayout(state)
                                }
                            }
                        }
                    }
                }
            }
        }
    }



    @Composable
    fun LoadingScreen() {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Box(Modifier.size(160.dp).clip(CircleShape).background(VaultGrey), contentAlignment = Alignment.Center) {
                    Icon(Icons.Default.Shield, null, tint = Gold, modifier = Modifier.size(64.dp))
                }
                Spacer(Modifier.height(32.dp))
                Text("VAULT", fontSize = 48.sp, fontWeight = FontWeight.Black, color = Gold, letterSpacing = 20.sp)
                Spacer(Modifier.height(16.dp))
                LinearProgressIndicator(color = Gold, trackColor = VaultGrey, modifier = Modifier.width(200.dp).height(2.dp))
            }
        }
    }

    @Composable
    fun Watermark() {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(
                "HANRYX VAULT",
                fontSize = 140.sp,
                fontWeight = FontWeight.Black,
                color = Gold.copy(alpha = 0.02f),
                modifier = Modifier.graphicsLayer(rotationZ = -30f)
            )
        }
    }

    @Composable
    fun LoginScreen(onLogin: (String) -> Unit) {
        var input by remember { mutableStateOf("") }

        val phase = remember { Animatable(0f) }
        LaunchedEffect(Unit) {
            delay(200)
            phase.animateTo(1f, tween(1800, easing = FastOutSlowInEasing))
        }

        val lockSpin = remember { Animatable(0f) }
        LaunchedEffect(Unit) {
            delay(100)
            lockSpin.animateTo(360f, tween(1200, easing = FastOutSlowInEasing))
        }

        val lockScale = remember { Animatable(2.5f) }
        LaunchedEffect(Unit) {
            delay(100)
            lockScale.animateTo(1f, spring(dampingRatio = 0.55f, stiffness = 300f))
        }

        val vaultGlow = rememberInfiniteTransition(label = "vaultGlow")
        val glowAlpha by vaultGlow.animateFloat(
            initialValue = 0.15f, targetValue = 0.4f,
            animationSpec = infiniteRepeatable(tween(2000, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ga"
        )
        val glowScale by vaultGlow.animateFloat(
            initialValue = 1f, targetValue = 1.15f,
            animationSpec = infiniteRepeatable(tween(2500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "gs"
        )

        val doorLeft = remember { Animatable(0f) }
        val doorRight = remember { Animatable(0f) }
        LaunchedEffect(Unit) {
            delay(600)
            launch { doorLeft.animateTo(-1f, tween(1000, easing = FastOutSlowInEasing)) }
            launch { doorRight.animateTo(1f, tween(1000, easing = FastOutSlowInEasing)) }
        }

        val titleScale = remember { Animatable(0.6f) }
        val titleAlpha = remember { Animatable(0f) }
        LaunchedEffect(Unit) {
            delay(800)
            launch { titleScale.animateTo(1f, spring(dampingRatio = 0.6f, stiffness = 250f)) }
            launch { titleAlpha.animateTo(1f, tween(600)) }
        }

        val formAlpha = remember { Animatable(0f) }
        val formSlide = remember { Animatable(40f) }
        LaunchedEffect(Unit) {
            delay(1500)
            launch { formAlpha.animateTo(1f, tween(500)) }
            launch { formSlide.animateTo(0f, tween(500, easing = FastOutSlowInEasing)) }
        }

        val subtitleAlpha = remember { Animatable(0f) }
        LaunchedEffect(Unit) {
            delay(1200)
            subtitleAlpha.animateTo(1f, tween(600))
        }

        val lockPulse = rememberInfiniteTransition(label = "lockPulse")
        val lockPulseScale by lockPulse.animateFloat(
            initialValue = 1f, targetValue = 1.08f,
            animationSpec = infiniteRepeatable(tween(1500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "lps"
        )
        val lockPulseAlpha by lockPulse.animateFloat(
            initialValue = 0.5f, targetValue = 0.9f,
            animationSpec = infiniteRepeatable(tween(1500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "lpa"
        )

        Box(Modifier.fillMaxSize().background(Color(0xFF0A0A0A)), contentAlignment = Alignment.Center) {
            Box(
                Modifier.size(300.dp)
                    .graphicsLayer(scaleX = glowScale, scaleY = glowScale, alpha = glowAlpha * phase.value)
                    .background(
                        Brush.radialGradient(
                            listOf(Gold.copy(alpha = 0.3f), Gold.copy(alpha = 0.05f), Color.Transparent),
                            radius = 400f
                        ),
                        CircleShape
                    )
            )

            Box(
                Modifier.fillMaxSize()
                    .graphicsLayer(alpha = (1f - phase.value).coerceIn(0f, 1f))
            ) {
                Box(
                    Modifier.fillMaxWidth(0.5f).fillMaxHeight().align(Alignment.CenterStart)
                        .graphicsLayer(translationX = doorLeft.value * 600f)
                        .background(
                            Brush.horizontalGradient(
                                listOf(Color(0xFF1A1A1A), Color(0xFF0D0D0D))
                            )
                        )
                ) {
                    Box(
                        Modifier.width(2.dp).fillMaxHeight().align(Alignment.CenterEnd)
                            .background(Gold.copy(alpha = 0.3f))
                    )
                }
                Box(
                    Modifier.fillMaxWidth(0.5f).fillMaxHeight().align(Alignment.CenterEnd)
                        .graphicsLayer(translationX = doorRight.value * 600f)
                        .background(
                            Brush.horizontalGradient(
                                listOf(Color(0xFF0D0D0D), Color(0xFF1A1A1A))
                            )
                        )
                ) {
                    Box(
                        Modifier.width(2.dp).fillMaxHeight().align(Alignment.CenterStart)
                            .background(Gold.copy(alpha = 0.3f))
                    )
                }
            }

            Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(32.dp)) {
                Box(contentAlignment = Alignment.Center) {
                    Box(
                        Modifier.size(80.dp)
                            .graphicsLayer(
                                scaleX = lockScale.value * lockPulseScale,
                                scaleY = lockScale.value * lockPulseScale,
                                alpha = lockPulseAlpha * phase.value
                            )
                            .background(Gold.copy(alpha = 0.08f), CircleShape)
                    )
                    Icon(
                        Icons.Default.Lock, null, tint = Gold,
                        modifier = Modifier.size(48.dp)
                            .graphicsLayer(
                                rotationZ = lockSpin.value,
                                scaleX = lockScale.value,
                                scaleY = lockScale.value,
                                alpha = lockPulseAlpha
                            )
                    )
                }
                Spacer(Modifier.height(24.dp))
                Text(
                    "VAULT",
                    fontSize = 72.sp,
                    fontWeight = FontWeight.Black,
                    color = Gold,
                    letterSpacing = 16.sp,
                    modifier = Modifier.graphicsLayer(
                        scaleX = titleScale.value,
                        scaleY = titleScale.value,
                        alpha = titleAlpha.value
                    )
                )
                Text(
                    "HANRYX RETAIL SYSTEMS",
                    fontSize = 10.sp,
                    color = Gold.copy(alpha = 0.4f),
                    letterSpacing = 6.sp,
                    modifier = Modifier.graphicsLayer(alpha = subtitleAlpha.value)
                )

                Spacer(Modifier.height(80.dp))

                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.graphicsLayer(
                        alpha = formAlpha.value,
                        translationY = formSlide.value
                    )
                ) {
                    OutlinedTextField(
                        value = input,
                        onValueChange = { input = it },
                        label = { Text("STAFF AUTH KEY", color = Gold, fontSize = 10.sp) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = Gold,
                            unfocusedBorderColor = Gold.copy(alpha = 0.2f),
                            focusedTextColor = Color.White,
                            cursorColor = Gold
                        ),
                        modifier = Modifier.fillMaxWidth(0.4f)
                    )

                    Spacer(Modifier.height(32.dp))

                    var btnPressed by remember { mutableStateOf(false) }
                    val btnScale by animateFloatAsState(
                        if (btnPressed) 0.95f else 1f,
                        spring(dampingRatio = 0.5f, stiffness = 800f), label = "btnTap"
                    )
                    LaunchedEffect(btnPressed) { if (btnPressed) { delay(120); btnPressed = false } }

                    Button(
                        onClick = {
                            btnPressed = true
                            if (input.isNotBlank()) onLogin(input)
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = VaultBlack),
                        modifier = Modifier.fillMaxWidth(0.4f).height(64.dp)
                            .graphicsLayer(scaleX = btnScale, scaleY = btnScale)
                    ) {
                        Text("AUTHORIZE", fontWeight = FontWeight.Black, letterSpacing = 4.sp)
                    }
                }
            }
        }
    }

    @OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
    @Composable
    fun MainPOSLayout(state: POSViewState) {
        var showCheckout by remember { mutableStateOf(false) }
        var showSuccess by remember { mutableStateOf(false) }
        var showVoiceInput by remember { mutableStateOf(false) }
        var showCounterfeitCamera by remember { mutableStateOf(false) }
        var showSettings by remember { mutableStateOf(false) }
        var showSalesHistory by remember { mutableStateOf(false) }
        var showCustomers by remember { mutableStateOf(false) }
        var showBulkScan by remember { mutableStateOf(false) }
        var showCardDeclined by remember { mutableStateOf(false) }
        var showEodConfirm by remember { mutableStateOf(false) }
        var showRepriceQueue by remember { mutableStateOf(false) }
        var showTradeIn by remember { mutableStateOf(false) }
        var showTabletOffer by remember { mutableStateOf(false) }
        var showCustomerSigning by remember { mutableStateOf(false) }
        var signingDecision by remember { mutableStateOf("") }
        var pendingSigningAction by remember { mutableStateOf<(() -> Unit)?>(null) }
        // Snapshot of trade items captured when signing begins so the screen
        // keeps showing them even after FinalizeTradeIn clears state.tradeInItems.
        var signingItems by remember { mutableStateOf<List<TradeInItem>>(emptyList()) }
        var showLotEval by remember { mutableStateOf(false) }
        var showArbitrageScout by remember { mutableStateOf(false) }
        var showMarketSearch by remember { mutableStateOf(false) }
        var showMainQrScanner by remember { mutableStateOf(false) }
        var showAddProductQrScanner by remember { mutableStateOf(false) }
        var priceCheckMode by remember { mutableStateOf(false) }
        var priceCheckQuery by remember { mutableStateOf("") }

        var hasCameraPermission by remember {
            mutableStateOf(
                ContextCompat.checkSelfPermission(this@MainActivity, android.Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
            )
        }
        var pendingScannerTarget by remember { mutableStateOf("") }
        val cameraPermissionLauncher = rememberLauncherForActivityResult(
            ActivityResultContracts.RequestPermission()
        ) { granted ->
            hasCameraPermission = granted
            if (granted) {
                when (pendingScannerTarget) {
                    "main" -> showMainQrScanner = true
                    "addProduct" -> showAddProductQrScanner = true
                }
            } else {
                Toast.makeText(this@MainActivity, "Camera permission is required for scanning. Grant it in Settings → Apps → HanryxVault → Permissions.", Toast.LENGTH_LONG).show()
            }
            pendingScannerTarget = ""
        }
        fun launchScannerWithPermission(target: String) {
            if (hasCameraPermission) {
                when (target) {
                    "main" -> showMainQrScanner = true
                    "addProduct" -> showAddProductQrScanner = true
                }
            } else {
                pendingScannerTarget = target
                cameraPermissionLauncher.launch(android.Manifest.permission.CAMERA)
            }
        }

        // Captures the print toggle from the checkout modal right before Zettle launches
        var pendingPrintReceipt by remember { mutableStateOf(true) }

        // Push clerk screen mode to kiosk whenever a modal screen opens or closes
        LaunchedEffect(showTradeIn, showLotEval, priceCheckMode) {
            val mode = when {
                showTradeIn     -> "trade_in"
                showLotEval     -> "lot_eval"
                priceCheckMode  -> "price_check"
                else            -> "cart"
            }
            viewModel.pushKioskMode(mode)
        }

        // Note: showCustomerSigning is intentionally NOT cleared here —
        // onRequestSigning sets showTradeIn=false AND showCustomerSigning=true
        // in the same frame; clearing it here would kill the signing screen.

        // Auto-show the tablet offer sheet when admin pushes a new trade-in
        LaunchedEffect(state.customerOffer?.ti_id, state.customerOfferStatus) {
            val offer = state.customerOffer
            if (offer != null && !offer.empty && state.customerOfferStatus == "pending") {
                showTabletOffer = true
            }
        }

        // ✅ Moved here so it shares scope with showSuccess/showCheckout/pendingPrintReceipt
        val paymentLauncher = rememberLauncherForActivityResult(
            contract = ActivityResultContracts.StartActivityForResult()
        ) { result ->
            val data = result.data ?: return@rememberLauncherForActivityResult
            val zettleResult = data.zettleResult()
            when (zettleResult) {
                is ZettleResult.Completed<*> -> {
                    val completed = CardReaderAction.fromPaymentResult(zettleResult)
                    val transactionId = completed.payload.transactionId ?: ""
                    viewModel.handleIntent(POSIntent.CompleteSale(
                        paymentMethod = PaymentMethod.CARD,
                        cardReference = transactionId,
                        shouldPrint = pendingPrintReceipt
                    ))
                    showSuccess = true
                    feedbackManager.playSuccessSound()
                }
                is ZettleResult.Cancelled -> {
                    Toast.makeText(this@MainActivity, "Payment cancelled", Toast.LENGTH_SHORT).show()
                }
                else -> {
                    showCardDeclined = true
                }
            }
        }
        // Separate launcher for Venmo QRC payments — result is handled via QrcAction, not CardReaderAction.
        val venmoPaymentLauncher = rememberLauncherForActivityResult(
            contract = ActivityResultContracts.StartActivityForResult()
        ) { result ->
            val data = result.data ?: return@rememberLauncherForActivityResult
            val zettleResult = data.zettleResult()
            when (zettleResult) {
                is ZettleResult.Completed<*> -> {
                    val payload = QrcAction.fromPaymentResult(zettleResult)
                    val transactionId = payload.transactionId ?: ""
                    viewModel.handleIntent(POSIntent.CompleteSale(
                        paymentMethod = PaymentMethod.VENMO,
                        cardReference = transactionId,
                        shouldPrint = pendingPrintReceipt
                    ))
                    showSuccess = true
                    feedbackManager.playSuccessSound()
                }
                is ZettleResult.Cancelled -> {
                    Toast.makeText(this@MainActivity, "Payment cancelled", Toast.LENGTH_SHORT).show()
                }
                else -> {
                    showCardDeclined = true
                }
            }
        }

        val snackbarHostState = remember { SnackbarHostState() }
        val cartSize = state.cartItems.sumOf { it.quantity }

        // Observe one-shot UI events (scan feedback, etc.)
        LaunchedEffect(Unit) {
            viewModel.uiEvents.collect { message ->
                snackbarHostState.currentSnackbarData?.dismiss()
                snackbarHostState.showSnackbar(message, duration = SnackbarDuration.Short)
            }
        }

        // Observe Zettle server-payment events
        LaunchedEffect(Unit) {
            viewModel.posEvents.collect { event ->
                when (event) {
                    is POSEvent.ZettlePaymentSuccess -> {
                        viewModel.handleIntent(POSIntent.CompleteSale(
                            paymentMethod = PaymentMethod.CARD,
                            shouldPrint = event.printReceipt
                        ))
                        showCheckout = false
                        showSuccess = true
                        feedbackManager.playSuccessSound()
                    }
                    is POSEvent.ZettlePaymentFailed -> {
                        Toast.makeText(this@MainActivity, "Payment failed: ${event.message}", Toast.LENGTH_LONG).show()
                    }
                    is POSEvent.ZettleNeedsLogin -> {
                        showCheckout = false
                        startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(event.loginUrl)))
                    }
                    is POSEvent.ZettleOfflineFallback -> {
                        // Card payments are now routed through the server API, not the on-device SDK.
                        // This event should no longer be emitted for card payments.
                        // If it fires unexpectedly, show a clear message.
                        showCheckout = false
                        Toast.makeText(
                            this@MainActivity,
                            "Card payment requires Zettle server connection — check Settings",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                }
            }
        }

        if (showMainQrScanner) {
            CompactQrScannerDialog(
                prompt = "Scan a product QR code or barcode",
                onDismiss = { showMainQrScanner = false },
                onResult = { code -> viewModel.scannerManager.onDataReceived(code) }
            )
        }

        if (showBulkScan) {
            BulkScanDialog(
                state = state,
                onDismiss = { showBulkScan = false },
                onCheckout = { showBulkScan = false; showCheckout = true; viewModel.pushKioskCheckoutStarted() },
                onScanItem = { code -> viewModel.scannerManager.onDataReceived(code) },
                onRemoveItem = { item -> viewModel.handleIntent(POSIntent.RemoveFromCart(item)) },
                identifyCardFromImage = { b64 ->
                    try {
                        val r = viewModel.callIdentifyCard(b64)
                        "${r.name} [${r.set_code}] - ${r.confidence} confidence"
                    } catch (e: Exception) { null }
                },
                onConditionCycle = { item ->
                    val next = cycleCondition(state.cardConditions[item.id] ?: "NM")
                    viewModel.handleIntent(POSIntent.SetCardCondition(item.id, next))
                }
            )
        }

        if (showSettings) {
            SettingsScreen(
                state = state,
                onDismiss = { showSettings = false },
                onToggleDrawer = { viewModel.handleIntent(POSIntent.SetCashDrawerEnabled(it)) },
                onToggleKickDrawer = { viewModel.handleIntent(POSIntent.SetKickDrawerEnabled(it)) },
                onUpdateQrData = { viewModel.handleIntent(POSIntent.SetCustomReceiptQrData(it)) },
                onUpdateDisclaimer = { viewModel.handleIntent(POSIntent.SetReceiptDisclaimer(it)) },
                onToggleRegisterMode = { viewModel.handleIntent(POSIntent.ToggleRegisterMode) },
                onSetPrinter = { addr, name -> viewModel.handleIntent(POSIntent.SetPrinter(addr, name)) },
                onSetPrinterMode = { mode -> viewModel.handleIntent(POSIntent.SetPrinterMode(mode)) },
                onSetPrinterEthernet = { host, port -> viewModel.handleIntent(POSIntent.SetPrinterEthernet(host, port)) },
                onTestPrinter = { viewModel.handleIntent(POSIntent.TestPrinter) },
                onTestKickDrawer = { viewModel.handleIntent(POSIntent.TestKickDrawer) },
                onSetReceiptLayout = { viewModel.handleIntent(POSIntent.SetReceiptLayout(it)) },
                onToggleReceiptQr = { viewModel.handleIntent(POSIntent.ToggleReceiptQr) },
                onUpdateQuickSalePreset = { i, label -> viewModel.handleIntent(POSIntent.UpdateQuickSalePreset(i, label)) },
                onForceSync = { viewModel.handleIntent(POSIntent.ForceSyncInventory) },
                onPingPi = { viewModel.handleIntent(POSIntent.PingPi) },
                onSetOpeningFloat = { amount -> viewModel.handleIntent(POSIntent.SetOpeningFloat(amount)) },
                onSetTradeBuyPct = { pct -> viewModel.handleIntent(POSIntent.SetTradeBuyPct(pct)) },
                onSetTradeCreditPct = { pct -> viewModel.handleIntent(POSIntent.SetTradeCreditPct(pct)) },
                onUpdateCustomerDisplayDisclosure = { text -> viewModel.handleIntent(POSIntent.UpdateCustomerDisplayDisclosure(text)) }
            )
            return
        }

        Box(Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxSize()) {
                // ── HEADER BAR ────────────────────────────────────────────────
                POSHeader(
                    state = state,
                    onSettingsClick = { showSettings = true },
                    onEodClick = { showEodConfirm = true },
                    onPrinterDotClick = { viewModel.handleIntent(POSIntent.RefreshPiPrinterStatus) },
                    onRepriceClick = { showRepriceQueue = true },
                    onArbitrageClick = { showArbitrageScout = true },
                    onMarketSearchClick = {
                        viewModel.handleIntent(POSIntent.ClearMarketSearch)
                        showMarketSearch = true
                    }
                )

                // ── Pi printer launch / queue banners ─────────────────────────
                // Yellow when /print/status reports the Pi printer is unreachable
                // in PI mode. Blue when one or more receipts are waiting in the
                // local retry queue. Both auto-clear once the underlying state
                // resolves, no operator action required.
                if (state.printerMode == "PI" && state.piPrinterReady == false) {
                    Surface(
                        color = Color(0xFF3B2A00),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Icon(Icons.Default.Warning, null, tint = Color(0xFFF59E0B), modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "Pi printer offline — ${state.piPrinterStatusMsg.ifBlank { "checking…" }}",
                                color = Color(0xFFF59E0B),
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.weight(1f)
                            )
                            // ── Quick-action chips ────────────────────────────
                            // Tapping these saves the operator from diving into
                            // Settings → POS Server URL when Tailscale drops.
                            // PING re-checks the active URL; USE LAN flips the
                            // saved URL to Constants.LAN_URL and re-checks.
                            Spacer(Modifier.width(8.dp))
                            Surface(
                                onClick = { viewModel.handleIntent(POSIntent.PingPi) },
                                color = Color(0xFF4A3700),
                                shape = RoundedCornerShape(6.dp)
                            ) {
                                Text(
                                    "PING",
                                    color = Color(0xFFF59E0B),
                                    fontSize = 10.sp,
                                    fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp)
                                )
                            }
                            Spacer(Modifier.width(6.dp))
                            Surface(
                                onClick = { viewModel.handleIntent(POSIntent.SwitchToLan) },
                                color = Color(0xFF1A3A1A),
                                shape = RoundedCornerShape(6.dp)
                            ) {
                                Text(
                                    "USE LAN",
                                    color = Color(0xFF4CAF50),
                                    fontSize = 10.sp,
                                    fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp)
                                )
                            }
                        }
                    }
                }
                if (state.pendingPrintJobs > 0) {
                    Surface(
                        color = Color(0xFF002B45),
                        modifier = Modifier.fillMaxWidth().clickable {
                            viewModel.handleIntent(POSIntent.RetryPendingPrints)
                        }
                    ) {
                        Row(
                            Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Icon(Icons.Default.Print, null, tint = Color(0xFF60A5FA), modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "${state.pendingPrintJobs} receipt${if (state.pendingPrintJobs == 1) "" else "s"} queued — auto-retry every 30 s (tap to retry now)",
                                color = Color(0xFF60A5FA),
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }
                }

                Row(Modifier.weight(1f)) {
                    // ── LEFT SIDEBAR ─ categories ──────────────────────────────
                    Column(
                        Modifier.width(96.dp).fillMaxHeight().background(VaultGrey).verticalScroll(rememberScrollState()),
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
                        Spacer(Modifier.height(8.dp))
                        CategorySideItem("All", state.selectedCategory == null, Icons.Default.GridView) {
                            viewModel.handleIntent(POSIntent.SelectCategory(null))
                        }
                        state.categories.forEach { cat ->
                            val icon = when (cat.lowercase()) {
                                "food", "drink" -> Icons.Default.LocalCafe
                                "electronics" -> Icons.Default.Memory
                                "clothing" -> Icons.Default.Checkroom
                                else -> Icons.Default.Label
                            }
                            CategorySideItem(cat.replaceFirstChar { it.uppercaseChar() }.take(8), state.selectedCategory == cat, icon) {
                                viewModel.handleIntent(POSIntent.SelectCategory(cat))
                            }
                        }
                        Spacer(Modifier.weight(1f))

                        // History button
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (showSalesHistory) Color(0xFF1A1A1A) else Color.Transparent)
                                .clickable { showSalesHistory = !showSalesHistory; showCustomers = false }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Icon(Icons.Default.Receipt, null, tint = if (showSalesHistory) Gold else Color(0xFF777777), modifier = Modifier.size(22.dp))
                            Text("History", color = if (showSalesHistory) Gold else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        // Customers / Store Credit button
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (showCustomers) Color(0xFF1A0D2A) else Color.Transparent)
                                .clickable { showCustomers = !showCustomers; showSalesHistory = false }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Icon(Icons.Default.People, null, tint = if (showCustomers) Color(0xFFCE93D8) else Color(0xFF777777), modifier = Modifier.size(22.dp))
                            Text("Credits", color = if (showCustomers) Color(0xFFCE93D8) else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        // Bulk scan mode button
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (state.isBulkScanMode) Color(0xFF0D1A0D) else Color.Transparent)
                                .clickable {
                                    viewModel.handleIntent(POSIntent.ToggleBulkScanMode)
                                    if (!state.isBulkScanMode) showBulkScan = true
                                }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Icon(Icons.Default.QrCodeScanner, null, tint = if (state.isBulkScanMode) Color(0xFF4ADE80) else Color(0xFF777777), modifier = Modifier.size(22.dp))
                            Text("Bulk", color = if (state.isBulkScanMode) Color(0xFF4ADE80) else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        // Trade-In mode button
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (state.isTradeInMode) Color(0xFF1A120A) else Color.Transparent)
                                .clickable {
                                    // Priority 1: if there is a pending pushed offer from admin and the
                                    // user dismissed its sheet, RE-OPEN that sheet — otherwise the offer
                                    // is unreachable until app restart (this was bug #1/#2/#3 combined).
                                    val pendingOffer = state.customerOffer
                                    if (pendingOffer != null && !pendingOffer.empty &&
                                        state.customerOfferStatus == "pending") {
                                        showTabletOffer = true
                                    } else {
                                        // Priority 2: normal trade-in flow.
                                        if (!state.isTradeInMode) viewModel.handleIntent(POSIntent.ActivateTradeIn)
                                        showTradeIn = true
                                    }
                                }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            val hasPendingPushedOffer = state.customerOffer != null &&
                                !(state.customerOffer?.empty ?: true) &&
                                state.customerOfferStatus == "pending"
                            Box(contentAlignment = Alignment.TopEnd) {
                                Icon(
                                    Icons.Default.SwapHoriz,
                                    null,
                                    tint = when {
                                        hasPendingPushedOffer -> Color(0xFF4ADE80)
                                        state.isTradeInMode -> Color(0xFFF59E0B)
                                        else -> Color(0xFF777777)
                                    },
                                    modifier = Modifier.size(22.dp)
                                )
                                val badgeCount = when {
                                    hasPendingPushedOffer -> state.customerOffer?.item_count ?: 0
                                    else -> state.tradeInItems.size
                                }
                                if (badgeCount > 0) {
                                    val badgeColor = if (hasPendingPushedOffer) Color(0xFF4ADE80) else Color(0xFFF59E0B)
                                    Box(
                                        Modifier.offset(x = 6.dp, y = (-4).dp).size(12.dp)
                                            .clip(CircleShape).background(badgeColor),
                                        contentAlignment = Alignment.Center
                                    ) { Text(badgeCount.toString(), color = Color.Black, fontSize = 7.sp, fontWeight = FontWeight.Black) }
                                }
                            }
                            Text(
                                if (hasPendingPushedOffer) "Offer!" else "Trade-In",
                                color = when {
                                    hasPendingPushedOffer -> Color(0xFF4ADE80)
                                    state.isTradeInMode -> Color(0xFFF59E0B)
                                    else -> Color(0xFF777777)
                                },
                                fontSize = 10.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                        Spacer(Modifier.height(4.dp))

                        // Lot Speed Evaluator button
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (state.isLotEvalMode) Color(0xFF0A1A0A) else Color.Transparent)
                                .clickable {
                                    if (state.isLotEvalMode) {
                                        showLotEval = true
                                    } else {
                                        viewModel.handleIntent(POSIntent.ToggleLotEvalMode)
                                        showLotEval = true
                                    }
                                }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            val lotPulse = rememberInfiniteTransition(label = "lotPulse")
                            val lotBadgeScale by lotPulse.animateFloat(
                                initialValue = 1f, targetValue = 1.35f,
                                animationSpec = infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "lbs"
                            )
                            Box(contentAlignment = Alignment.TopEnd) {
                                Icon(Icons.Default.ViewList, null, tint = if (state.isLotEvalMode) Color(0xFF4ADE80) else Color(0xFF777777), modifier = Modifier.size(22.dp))
                                if (state.lotEvalItems.isNotEmpty()) {
                                    Box(
                                        Modifier.offset(x = 6.dp, y = (-4).dp).size(12.dp)
                                            .graphicsLayer(scaleX = lotBadgeScale, scaleY = lotBadgeScale)
                                            .clip(CircleShape).background(Color(0xFF4ADE80)),
                                        contentAlignment = Alignment.Center
                                    ) { Text(state.lotEvalItems.size.toString(), color = Color.Black, fontSize = 7.sp, fontWeight = FontWeight.Black) }
                                }
                            }
                            Text("Lot Eval", color = if (state.isLotEvalMode) Color(0xFF4ADE80) else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        HorizontalDivider(Modifier.padding(horizontal = 12.dp), color = Color(0xFF2A2A2A), thickness = 0.5.dp)
                        Spacer(Modifier.height(4.dp))

                        val repriceCount = state.repriceQueue.size
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (repriceCount > 0) Color(0xFF1A1200) else Color.Transparent)
                                .clickable { showRepriceQueue = true }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Box(contentAlignment = Alignment.TopEnd) {
                                Icon(Icons.Default.TrendingUp, null, tint = if (repriceCount > 0) Color(0xFFF59E0B) else Color(0xFF777777), modifier = Modifier.size(22.dp))
                                if (repriceCount > 0) {
                                    Box(
                                        Modifier.offset(x = 6.dp, y = (-4).dp).size(12.dp)
                                            .clip(CircleShape).background(Color(0xFFF59E0B)),
                                        contentAlignment = Alignment.Center
                                    ) { Text(repriceCount.toString(), color = Color.Black, fontSize = 7.sp, fontWeight = FontWeight.Black) }
                                }
                            }
                            Text("Reprice", color = if (repriceCount > 0) Color(0xFFF59E0B) else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        val scoutItems = state.repriceQueue.filter { it.pctChange > 0 }
                        val scoutCount = scoutItems.size
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .background(if (scoutCount > 0) Color(0xFF0A1A0A) else Color.Transparent)
                                .clickable { showArbitrageScout = true }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Box(contentAlignment = Alignment.TopEnd) {
                                Icon(Icons.Default.AttachMoney, null, tint = if (scoutCount > 0) Color(0xFF4ADE80) else Color(0xFF777777), modifier = Modifier.size(22.dp))
                                if (scoutCount > 0) {
                                    Box(
                                        Modifier.offset(x = 6.dp, y = (-4).dp).size(12.dp)
                                            .clip(CircleShape).background(Color(0xFF4ADE80)),
                                        contentAlignment = Alignment.Center
                                    ) { Text(scoutCount.toString(), color = Color.Black, fontSize = 7.sp, fontWeight = FontWeight.Black) }
                                }
                            }
                            Text("Scout", color = if (scoutCount > 0) Color(0xFF4ADE80) else Color(0xFF777777), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .clickable { viewModel.handleIntent(POSIntent.ShowOcrScanDialog) }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Icon(Icons.Default.DocumentScanner, null, tint = Color(0xFF29B6F6), modifier = Modifier.size(22.dp))
                            Text("Scan", color = Color(0xFF29B6F6), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(4.dp))

                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier
                                .clip(RoundedCornerShape(10.dp))
                                .clickable { showCounterfeitCamera = true }
                                .padding(vertical = 6.dp, horizontal = 4.dp)
                        ) {
                            Icon(Icons.Default.VerifiedUser, null, tint = Color(0xFF00E676), modifier = Modifier.size(22.dp))
                            Text("Auth", color = Color(0xFF00E676), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                        }
                        Spacer(Modifier.height(8.dp))

                        if (state.isRegisterMode) {
                            Box(
                                Modifier.size(32.dp).clip(CircleShape).background(Color(0xFF5A2D00)),
                                contentAlignment = Alignment.Center
                            ) {
                                Icon(Icons.Default.Lock, null, tint = Color(0xFFFF6B00), modifier = Modifier.size(16.dp))
                            }
                            Spacer(Modifier.height(4.dp))
                        }
                        IconButton(onClick = {
                            if (!state.sdkLoggedIn) {
                                // Open Settings so user can log in via ZETTLE ACCOUNT section
                                showSettings = true
                            } else {
                                Toast.makeText(this@MainActivity, "Zettle card reader ready", Toast.LENGTH_SHORT).show()
                            }
                        }) {
                            Icon(Icons.Default.CreditCard, null, tint = if (state.sdkLoggedIn) Gold else Color.Gray, modifier = Modifier.size(20.dp))
                        }
                        Spacer(Modifier.height(8.dp))
                    }

                    // ── CENTER ─ search + product grid ─────────────────────────
                    var showAddProduct by remember { mutableStateOf(false) }
                    var addProductQrCode by remember { mutableStateOf("") }
                    if (showAddProductQrScanner) {
                        CompactQrScannerDialog(
                            prompt = "Scan product QR code or barcode",
                            onDismiss = { showAddProductQrScanner = false },
                            onResult = { code -> addProductQrCode = code }
                        )
                    }
                    if (showAddProduct) {
                        AddProductDialog(
                            categories = state.categories,
                            qrCode = addProductQrCode,
                            onScanQrCode = { launchScannerWithPermission("addProduct") },
                            onConfirm = { name, price, category, qrCode ->
                                viewModel.handleIntent(POSIntent.AddProduct(name, price, category, qrCode))
                                showAddProduct = false
                                addProductQrCode = ""
                            },
                            onDismiss = { showAddProduct = false; addProductQrCode = "" }
                        )
                    }

                    Box(Modifier.weight(1f).background(VaultBlack)) {
                    Column(Modifier.fillMaxSize().padding(horizontal = 20.dp, vertical = 16.dp)) {
                        // Offline banner
                        androidx.compose.animation.AnimatedVisibility(visible = state.isOffline) {
                            Surface(
                                color = Color(0xFF3D2800),
                                shape = RoundedCornerShape(8.dp),
                                modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp)
                            ) {
                                Row(
                                    Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Icon(Icons.Default.WifiOff, null, tint = Color(0xFFFFB74D), modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text(
                                        buildString {
                                            append("OFFLINE — using cached inventory")
                                            if (state.pendingSalesCount > 0) append("  ·  ${state.pendingSalesCount} sale${if (state.pendingSalesCount != 1) "s" else ""} queued")
                                            if (state.pendingProductsCount > 0) append("  ·  ${state.pendingProductsCount} product${if (state.pendingProductsCount != 1) "s" else ""} pending")
                                        },
                                        color = Color(0xFFFFB74D),
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                    Spacer(Modifier.weight(1f))
                                    Text("WILL AUTO-SYNC", color = Color(0xFF8D6E00), fontSize = 9.sp, fontWeight = FontWeight.Bold)
                                }
                            }
                        }

                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            OutlinedTextField(
                                value = if (priceCheckMode) priceCheckQuery else state.searchQuery,
                                onValueChange = {
                                    if (priceCheckMode) {
                                        priceCheckQuery = it
                                    } else {
                                        viewModel.handleIntent(POSIntent.SearchInventory(it))
                                    }
                                },
                                placeholder = {
                                    Text(
                                        if (priceCheckMode) "Search TCGPlayer + eBay prices…" else "Search or scan products…",
                                        color = Color.Gray, fontSize = 14.sp
                                    )
                                },
                                leadingIcon = {
                                    Icon(
                                        if (priceCheckMode) Icons.Default.AttachMoney else Icons.Default.Search,
                                        null,
                                        tint = if (priceCheckMode) Color(0xFF4ADE80) else Color.Gray,
                                        modifier = Modifier.size(18.dp)
                                    )
                                },
                                trailingIcon = {
                                    if (priceCheckMode) {
                                        Row {
                                            if (priceCheckQuery.isNotEmpty()) {
                                                IconButton(onClick = {
                                                    priceCheckQuery = ""
                                                    viewModel.handleIntent(POSIntent.ClearMarketSearch)
                                                }) {
                                                    Icon(Icons.Default.Clear, null, tint = Color.Gray, modifier = Modifier.size(16.dp))
                                                }
                                            }
                                            IconButton(onClick = {
                                                if (priceCheckQuery.isNotBlank()) {
                                                    viewModel.handleIntent(POSIntent.SearchMarketPrice(priceCheckQuery.trim(), "", ""))
                                                }
                                            }) {
                                                Icon(Icons.Default.Search, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(20.dp))
                                            }
                                        }
                                    } else if (state.searchQuery.isNotEmpty()) {
                                        IconButton(onClick = { viewModel.handleIntent(POSIntent.SearchInventory("")) }) {
                                            Icon(Icons.Default.Clear, null, tint = Color.Gray, modifier = Modifier.size(16.dp))
                                        }
                                    } else {
                                        Row {
                                            IconButton(onClick = { showVoiceInput = true }) {
                                                Icon(Icons.Default.Mic, null, tint = Color(0xFF7C4DFF), modifier = Modifier.size(20.dp))
                                            }
                                            IconButton(onClick = { launchScannerWithPermission("main") }) {
                                                Icon(Icons.Default.QrCodeScanner, null, tint = Gold, modifier = Modifier.size(20.dp))
                                            }
                                        }
                                    }
                                },
                                keyboardActions = androidx.compose.foundation.text.KeyboardActions(
                                    onSearch = {
                                        if (priceCheckMode && priceCheckQuery.isNotBlank()) {
                                            viewModel.handleIntent(POSIntent.SearchMarketPrice(priceCheckQuery.trim(), "", ""))
                                        }
                                    }
                                ),
                                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                                    imeAction = if (priceCheckMode) androidx.compose.ui.text.input.ImeAction.Search else androidx.compose.ui.text.input.ImeAction.Done
                                ),
                                modifier = Modifier.weight(1f).height(52.dp),
                                singleLine = true,
                                shape = RoundedCornerShape(12.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    focusedBorderColor = if (priceCheckMode) Color(0xFF4ADE80) else Gold,
                                    unfocusedBorderColor = if (priceCheckMode) Color(0xFF4ADE80).copy(alpha = 0.5f) else Color(0xFF333333),
                                    focusedContainerColor = if (priceCheckMode) Color(0xFF0D1A0D) else VaultSurface,
                                    unfocusedContainerColor = if (priceCheckMode) Color(0xFF0D1A0D) else VaultSurface
                                )
                            )
                            Spacer(Modifier.width(8.dp))
                            Surface(
                                modifier = Modifier
                                    .height(52.dp)
                                    .clip(RoundedCornerShape(12.dp))
                                    .clickable {
                                        priceCheckMode = !priceCheckMode
                                        if (!priceCheckMode) {
                                            priceCheckQuery = ""
                                            viewModel.handleIntent(POSIntent.ClearMarketSearch)
                                        }
                                    },
                                color = if (priceCheckMode) Color(0xFF4ADE80) else Color(0xFF1A1A1A),
                                shape = RoundedCornerShape(12.dp),
                                border = BorderStroke(1.dp, if (priceCheckMode) Color(0xFF4ADE80) else Color(0xFF333333))
                            ) {
                                Row(
                                    Modifier.padding(horizontal = 12.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Icon(
                                        Icons.Default.AttachMoney, null,
                                        tint = if (priceCheckMode) Color.Black else Color(0xFF4ADE80),
                                        modifier = Modifier.size(18.dp)
                                    )
                                    Spacer(Modifier.width(4.dp))
                                    Text(
                                        "Price",
                                        color = if (priceCheckMode) Color.Black else Color(0xFF4ADE80),
                                        fontSize = 12.sp,
                                        fontWeight = FontWeight.Bold
                                    )
                                }
                            }
                        }

                        if (priceCheckMode) {
                            Spacer(Modifier.height(8.dp))
                            val pcResult = state.marketSearchResult
                            val pcSearching = state.isMarketSearching
                            if (pcSearching) {
                                Surface(
                                    Modifier.fillMaxWidth(),
                                    color = Color(0xFF0D1A0D),
                                    shape = RoundedCornerShape(10.dp)
                                ) {
                                    Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                        CircularProgressIndicator(modifier = Modifier.size(16.dp), color = Color(0xFF4ADE80), strokeWidth = 1.5.dp)
                                        Spacer(Modifier.width(10.dp))
                                        Text("Searching TCGPlayer + eBay…", color = Color(0xFF4ADE80).copy(alpha = 0.7f), fontSize = 12.sp)
                                    }
                                }
                            } else if (pcResult != null) {
                                Surface(
                                    Modifier.fillMaxWidth(),
                                    color = Color(0xFF0D1A0D),
                                    shape = RoundedCornerShape(12.dp),
                                    border = BorderStroke(1.dp, Color(0xFF4ADE80).copy(alpha = 0.3f))
                                ) {
                                    Column(Modifier.padding(12.dp)) {
                                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                            Text(pcResult.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                                            Surface(
                                                color = when (pcResult.confidence) {
                                                    "HIGH" -> Color(0xFF4ADE80).copy(alpha = 0.15f)
                                                    "MEDIUM" -> Color(0xFFFFD700).copy(alpha = 0.15f)
                                                    else -> Color(0xFFFF6B6B).copy(alpha = 0.15f)
                                                },
                                                shape = RoundedCornerShape(4.dp)
                                            ) {
                                                Text(
                                                    pcResult.confidence,
                                                    color = when (pcResult.confidence) {
                                                        "HIGH" -> Color(0xFF4ADE80)
                                                        "MEDIUM" -> Color(0xFFFFD700)
                                                        else -> Color(0xFFFF6B6B)
                                                    },
                                                    fontSize = 9.sp,
                                                    fontWeight = FontWeight.Bold,
                                                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                                                )
                                            }
                                        }
                                        Spacer(Modifier.height(8.dp))
                                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                                Text("MARKET", color = Color(0xFF888888), fontSize = 9.sp, letterSpacing = 1.sp)
                                                Text(
                                                    "$${String.format("%.2f", pcResult.weighted_avg)}",
                                                    color = Color(0xFF4ADE80),
                                                    fontWeight = FontWeight.Black,
                                                    fontSize = 22.sp
                                                )
                                            }
                                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                                Text("TRADE", color = Color(0xFF888888), fontSize = 9.sp, letterSpacing = 1.sp)
                                                Text(
                                                    "$${String.format("%.2f", pcResult.trade_value)}",
                                                    color = Color(0xFFF59E0B),
                                                    fontWeight = FontWeight.Black,
                                                    fontSize = 22.sp
                                                )
                                            }
                                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                                Text("SAMPLES", color = Color(0xFF888888), fontSize = 9.sp, letterSpacing = 1.sp)
                                                Text(
                                                    "${pcResult.total_samples}",
                                                    color = Color.White,
                                                    fontWeight = FontWeight.Bold,
                                                    fontSize = 22.sp
                                                )
                                            }
                                        }
                                        Spacer(Modifier.height(6.dp))
                                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                            val srcList = listOf(
                                                "TCGPlayer" to pcResult.sources.pokemontcg,
                                                "eBay" to pcResult.sources.ebay,
                                                "Local" to pcResult.sources.local
                                            )
                                            srcList.forEach { (srcName, srcData) ->
                                                if (srcData.avg > 0) {
                                                    Surface(
                                                        color = Color(0xFF111111),
                                                        shape = RoundedCornerShape(6.dp)
                                                    ) {
                                                        Text(
                                                            "$srcName: $${String.format("%.2f", srcData.avg)} (${srcData.count})",
                                                            color = Color(0xFF999999),
                                                            fontSize = 10.sp,
                                                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                                                        )
                                                    }
                                                }
                                            }
                                        }
                                        Spacer(Modifier.height(6.dp))
                                        val selVariant = state.marketVariants.getOrNull(state.selectedVariantIdx)
                                        val cartName = if (selVariant != null) "${selVariant.name} - ${selVariant.set_name}" else pcResult.name
                                        val cartPrice = if (selVariant != null) { if (selVariant.weighted > 0) selVariant.weighted else selVariant.tcg_best } else pcResult.weighted_avg
                                        val cartSetId = selVariant?.set_id ?: ""
                                        val cartQrSuffix = selVariant?.id?.ifBlank { null } ?: System.currentTimeMillis().toString()
                                        val tradeVal = if (selVariant != null) cartPrice * 0.65 else pcResult.trade_value
                                        if (selVariant != null) {
                                            Text("Selected: ${selVariant.name} · ${selVariant.set_name}", color = Color(0xFF4ADE80).copy(alpha = 0.7f), fontSize = 9.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                            Spacer(Modifier.height(4.dp))
                                        }
                                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                                            Button(
                                                onClick = {
                                                    val qr = "PC_${cartQrSuffix}_${System.currentTimeMillis()}"
                                                    val product = ProductEntity(qrCode = qr, name = cartName, price = cartPrice, category = "Pokemon", setCode = cartSetId, stockQuantity = 1)
                                                    viewModel.handleIntent(POSIntent.AddToCart(product))
                                                },
                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF4ADE80)),
                                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp),
                                                modifier = Modifier.height(30.dp),
                                                shape = RoundedCornerShape(6.dp)
                                            ) {
                                                Icon(Icons.Default.AddShoppingCart, null, tint = Color.Black, modifier = Modifier.size(13.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("+ Cart", color = Color.Black, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                            }
                                            Button(
                                                onClick = {
                                                    val qr = "PC_${cartQrSuffix}_${System.currentTimeMillis()}"
                                                    val product = ProductEntity(qrCode = qr, name = cartName, price = cartPrice, category = "Pokemon", setCode = cartSetId, stockQuantity = 1)
                                                    viewModel.handleIntent(POSIntent.AddToCart(product))
                                                    showCheckout = true
                                                },
                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B)),
                                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp),
                                                modifier = Modifier.height(30.dp),
                                                shape = RoundedCornerShape(6.dp)
                                            ) {
                                                Icon(Icons.Default.PointOfSale, null, tint = Color.Black, modifier = Modifier.size(13.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("Cash Out", color = Color.Black, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                            }
                                            Button(
                                                onClick = {
                                                    viewModel.handleIntent(POSIntent.ManualTradeInAdd(cartName, tradeVal))
                                                    showTradeIn = true
                                                },
                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B).copy(alpha = 0.15f)),
                                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp),
                                                modifier = Modifier.height(30.dp),
                                                shape = RoundedCornerShape(6.dp)
                                            ) {
                                                Icon(Icons.Default.SwapHoriz, null, tint = Color(0xFFF59E0B), modifier = Modifier.size(13.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("Trade", color = Color(0xFFF59E0B), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                                if (state.tradeInItems.isNotEmpty()) {
                                                    Spacer(Modifier.width(4.dp))
                                                    Surface(color = Color(0xFFF59E0B), shape = CircleShape, modifier = Modifier.size(16.dp)) {
                                                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                                            Text("${state.tradeInItems.size}", color = Color.Black, fontSize = 8.sp, fontWeight = FontWeight.Black)
                                                        }
                                                    }
                                                }
                                            }
                                            Spacer(Modifier.weight(1f))
                                            TextButton(onClick = {
                                                viewModel.handleIntent(POSIntent.ClearMarketSearch)
                                                showMarketSearch = true
                                            }) {
                                                Text("Details →", color = Gold, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                                            }
                                        }
                                    }
                                }

                                val variants = state.marketVariants
                                val isLoadingVariants = state.isLoadingVariants
                                if (isLoadingVariants) {
                                    Spacer(Modifier.height(6.dp))
                                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(horizontal = 4.dp)) {
                                        CircularProgressIndicator(modifier = Modifier.size(12.dp), color = Color(0xFF4ADE80).copy(alpha = 0.5f), strokeWidth = 1.5.dp)
                                        Spacer(Modifier.width(8.dp))
                                        Text("Loading printings…", color = Color(0xFF4ADE80).copy(alpha = 0.5f), fontSize = 11.sp)
                                    }
                                } else if (variants.isNotEmpty()) {
                                    Spacer(Modifier.height(8.dp))
                                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                        Text("PRINTINGS", color = Color(0xFF666666), fontSize = 9.sp, letterSpacing = 1.sp, fontWeight = FontWeight.Bold)
                                        Spacer(Modifier.width(6.dp))
                                        Surface(color = Color(0xFF4ADE80).copy(alpha = 0.1f), shape = RoundedCornerShape(4.dp)) {
                                            Text("${variants.size}", color = Color(0xFF4ADE80), fontSize = 9.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 1.dp))
                                        }
                                        Spacer(Modifier.weight(1f))
                                        Text("← swipe →", color = Color(0xFF444444), fontSize = 9.sp)
                                    }
                                    Spacer(Modifier.height(6.dp))
                                    val variantPagerState = androidx.compose.foundation.pager.rememberPagerState(pageCount = { variants.size })
                                    androidx.compose.foundation.pager.HorizontalPager(
                                        state = variantPagerState,
                                        contentPadding = PaddingValues(end = 40.dp),
                                        pageSpacing = 8.dp,
                                        modifier = Modifier.fillMaxWidth().height(140.dp)
                                    ) { page ->
                                        val variant = variants[page]
                                        val isSelected = state.selectedVariantIdx == page
                                        val cardEnter = remember { Animatable(0f) }
                                        LaunchedEffect(Unit) { cardEnter.animateTo(1f, tween(350, delayMillis = page * 40)) }
                                        Surface(
                                            modifier = Modifier.fillMaxSize()
                                                .graphicsLayer(
                                                    alpha = cardEnter.value,
                                                    translationY = (1f - cardEnter.value) * 20f
                                                ),
                                            color = if (isSelected) Color(0xFF142014) else Color(0xFF111111),
                                            shape = RoundedCornerShape(10.dp),
                                            border = BorderStroke(1.dp, if (isSelected) Color(0xFF4ADE80).copy(alpha = 0.5f) else Color(0xFF2A2A2A)),
                                            onClick = { viewModel.handleIntent(POSIntent.SelectMarketVariant(page)) }
                                        ) {
                                            Row(Modifier.padding(10.dp)) {
                                                if (variant.image_small.isNotBlank()) {
                                                    Surface(
                                                        color = Color(0xFF0A0A0A),
                                                        shape = RoundedCornerShape(6.dp),
                                                        modifier = Modifier.width(72.dp).fillMaxHeight()
                                                    ) {
                                                        AsyncImage(
                                                            model = variant.image_small,
                                                            contentDescription = variant.name,
                                                            modifier = Modifier.fillMaxSize().padding(2.dp)
                                                        )
                                                    }
                                                    Spacer(Modifier.width(10.dp))
                                                }
                                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.SpaceBetween) {
                                                    Column {
                                                        Text(variant.name, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold, maxLines = 2, overflow = TextOverflow.Ellipsis, lineHeight = 14.sp)
                                                        if (variant.set_name.isNotBlank()) {
                                                            Spacer(Modifier.height(2.dp))
                                                            Text(variant.set_name, color = Color(0xFF666666), fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                                        }
                                                        if (variant.number.isNotBlank() || variant.rarity.isNotBlank()) {
                                                            Text(
                                                                listOfNotNull(
                                                                    if (variant.number.isNotBlank()) "#${variant.number}" else null,
                                                                    variant.rarity.ifBlank { null }
                                                                ).joinToString(" · "),
                                                                color = Color(0xFF555555), fontSize = 9.sp
                                                            )
                                                        }
                                                    }
                                                    Spacer(Modifier.height(4.dp))
                                                    val varPrice = if (variant.weighted > 0) variant.weighted else variant.tcg_best
                                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                                        if (varPrice > 0) {
                                                            Text("$${String.format("%.2f", varPrice)}", color = Color(0xFF4ADE80), fontWeight = FontWeight.Black, fontSize = 16.sp)
                                                            Spacer(Modifier.width(8.dp))
                                                        }
                                                    }
                                                    val tradePrice = varPrice * 0.65
                                                    if (varPrice > 0) {
                                                        Spacer(Modifier.height(2.dp))
                                                        Text("Trade: $${String.format("%.2f", tradePrice)}", color = Color(0xFFF59E0B), fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                                                        Spacer(Modifier.height(4.dp))
                                                        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                                            Button(
                                                                onClick = {
                                                                    viewModel.handleIntent(POSIntent.ManualTradeInAdd(
                                                                        "${variant.name} - ${variant.set_name}",
                                                                        tradePrice
                                                                    ))
                                                                    viewModel.handleIntent(POSIntent.SelectMarketVariant(page))
                                                                },
                                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B)),
                                                                contentPadding = PaddingValues(horizontal = 6.dp, vertical = 2.dp),
                                                                modifier = Modifier.height(26.dp),
                                                                shape = RoundedCornerShape(6.dp)
                                                            ) {
                                                                Icon(Icons.Default.SwapHoriz, null, tint = Color.Black, modifier = Modifier.size(11.dp))
                                                                Spacer(Modifier.width(2.dp))
                                                                Text("Trade $${String.format("%.2f", tradePrice)}", color = Color.Black, fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                                            }
                                                            Button(
                                                                onClick = {
                                                                    val qr = "PC_${variant.id.ifBlank { System.currentTimeMillis().toString() }}_${System.currentTimeMillis()}"
                                                                    val product = ProductEntity(qrCode = qr, name = "${variant.name} - ${variant.set_name}", price = varPrice, category = "Pokemon", setCode = variant.set_id, stockQuantity = 1)
                                                                    viewModel.handleIntent(POSIntent.AddToCart(product))
                                                                    showCheckout = true
                                                                },
                                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF4ADE80)),
                                                                contentPadding = PaddingValues(horizontal = 6.dp, vertical = 2.dp),
                                                                modifier = Modifier.height(26.dp),
                                                                shape = RoundedCornerShape(6.dp)
                                                            ) {
                                                                Icon(Icons.Default.PointOfSale, null, tint = Color.Black, modifier = Modifier.size(11.dp))
                                                                Spacer(Modifier.width(2.dp))
                                                                Text("Cash Out", color = Color.Black, fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    Row(Modifier.fillMaxWidth().padding(top = 6.dp), horizontalArrangement = Arrangement.Center) {
                                        repeat(variants.size.coerceAtMost(10)) { dot ->
                                            val dotActive = variantPagerState.currentPage == dot
                                            val dotAlpha by animateFloatAsState(if (dotActive) 1f else 0.3f, tween(200), label = "dot$dot")
                                            val dotWidth by animateDpAsState(if (dotActive) 12.dp else 4.dp, tween(200), label = "dotW$dot")
                                            Box(
                                                Modifier.padding(horizontal = 2.dp).height(4.dp).width(dotWidth)
                                                    .clip(CircleShape).background(Color(0xFF4ADE80).copy(alpha = dotAlpha))
                                            )
                                        }
                                    }
                                }
                            } else if (state.marketSearchError.isNotBlank()) {
                                Surface(
                                    Modifier.fillMaxWidth(),
                                    color = Color(0xFF1A0D0D),
                                    shape = RoundedCornerShape(10.dp)
                                ) {
                                    Text(state.marketSearchError, color = Color(0xFFFF6B6B), fontSize = 12.sp, modifier = Modifier.padding(12.dp))
                                }
                            }
                        }

                        Spacer(Modifier.height(if (priceCheckMode && (state.marketSearchResult != null || state.isMarketSearching)) 8.dp else 16.dp))

                        // Product grid
                        if (state.searchResults.isEmpty()) {
                            Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                                val emptyProdFloat = rememberInfiniteTransition(label = "emptyProd")
                                val emptyProdY by emptyProdFloat.animateFloat(
                                    initialValue = -4f, targetValue = 4f,
                                    animationSpec = infiniteRepeatable(tween(2500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "epy"
                                )
                                Column(
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                    modifier = Modifier
                                        .graphicsLayer(translationY = emptyProdY)
                                        // Long-press anywhere on this empty-state to
                                        // report the missing search query to the Pi
                                        // discovery queue. Short-tap is a no-op so the
                                        // operator can't fire it accidentally.
                                        .combinedClickable(
                                            onClick = {},
                                            onLongClick = {
                                                if (state.searchQuery.isNotBlank()) {
                                                    viewModel.handleIntent(
                                                        POSIntent.ReportMissingCard(state.searchQuery)
                                                    )
                                                }
                                            }
                                        )
                                ) {
                                    Icon(Icons.Default.Inventory2, null, tint = Color(0xFF333333), modifier = Modifier.size(56.dp))
                                    Spacer(Modifier.height(12.dp))
                                    Text("No products found", color = Color(0xFF555555), fontSize = 14.sp)
                                    if (state.searchQuery.isNotEmpty()) {
                                        Text("Try a different search", color = Color(0xFF444444), fontSize = 12.sp)
                                        Spacer(Modifier.height(4.dp))
                                        Text(
                                            "long-press to report missing card",
                                            color = Color(0xFF333333),
                                            fontSize = 10.sp,
                                            fontStyle = androidx.compose.ui.text.font.FontStyle.Italic
                                        )
                                    }
                                }
                            }
                        } else {
                            LazyVerticalGrid(
                                columns = GridCells.Adaptive(minSize = 180.dp),
                                horizontalArrangement = Arrangement.spacedBy(12.dp),
                                verticalArrangement = Arrangement.spacedBy(12.dp),
                                modifier = Modifier.weight(1f)
                            ) {
                                items(state.searchResults, key = { it.id }) { product ->
                                    ProductCard(product,
                                        onClick = { viewModel.handleIntent(POSIntent.AddToCart(product)) },
                                        onLongClick = { viewModel.handleIntent(POSIntent.OpenVariantPicker(product)) }
                                    )
                                }
                            }
                        }
                    }

                    val fabEnter = remember { Animatable(0f) }
                    LaunchedEffect(Unit) { delay(300); fabEnter.animateTo(1f, spring(dampingRatio = 0.6f, stiffness = 400f)) }
                    FloatingActionButton(
                        onClick = { showAddProduct = true },
                        modifier = Modifier
                            .align(Alignment.BottomEnd)
                            .padding(end = 20.dp, bottom = 20.dp)
                            .size(48.dp)
                            .graphicsLayer(scaleX = fabEnter.value, scaleY = fabEnter.value, alpha = fabEnter.value),
                        containerColor = Gold,
                        contentColor = VaultBlack,
                        shape = RoundedCornerShape(14.dp),
                        elevation = FloatingActionButtonDefaults.elevation(8.dp)
                    ) {
                        Icon(Icons.Default.Add, contentDescription = "Add product", modifier = Modifier.size(22.dp))
                    }
                    } // end Box

                    // ── RIGHT ─ cart / ticket ──────────────────────────────────
                    Box(Modifier.width(340.dp).fillMaxHeight()) {
                    Column(
                        Modifier.fillMaxSize().background(VaultGrey)
                    ) {
                        Row(
                            Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 16.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text("ORDER", fontWeight = FontWeight.Black, color = Gold, fontSize = 13.sp, letterSpacing = 3.sp)
                            AnimatedVisibility(
                                visible = cartSize > 0,
                                enter = fadeIn(tween(200)) + slideInHorizontally(tween(250)) { it / 2 },
                                exit = fadeOut(tween(150)) + slideOutHorizontally(tween(200)) { it / 2 }
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    val animatedCount by animateIntAsState(cartSize, tween(300), label = "cartCount")
                                    Text("$animatedCount item${if (animatedCount != 1) "s" else ""}", color = Color.Gray, fontSize = 12.sp)
                                    Spacer(Modifier.width(12.dp))
                                    TextButton(
                                        onClick = { viewModel.handleIntent(POSIntent.ClearCart) },
                                        contentPadding = PaddingValues(0.dp)
                                    ) {
                                        Text("Clear", color = Color(0xFF666666), fontSize = 11.sp)
                                    }
                                }
                            }
                        }

                        HorizontalDivider(color = Color(0xFF2A2A2A))

                        // ── QUICK SALE STRIP ───────────────────────────────────
                        var quickSaleTarget by remember { mutableStateOf<QuickSalePreset?>(null) }

                        if (quickSaleTarget != null) {
                            QuickSaleAmountDialog(
                                preset = quickSaleTarget!!,
                                onConfirm = { amount ->
                                    viewModel.handleIntent(POSIntent.AddQuickSale(quickSaleTarget!!.label, amount))
                                    quickSaleTarget = null
                                },
                                onDismiss = { quickSaleTarget = null }
                            )
                        }

                        Row(
                            Modifier
                                .fillMaxWidth()
                                .background(Color(0xFF111111))
                                .padding(horizontal = 8.dp, vertical = 4.dp),
                            horizontalArrangement = Arrangement.spacedBy(4.dp)
                        ) {
                            state.quickSalePresets.forEach { preset ->
                                Box(
                                    Modifier.weight(1f)
                                        .clip(RoundedCornerShape(6.dp))
                                        .background(Color(0xFF1A1A1A))
                                        .clickable { quickSaleTarget = preset }
                                        .padding(vertical = 6.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(preset.label, color = Color(0xFF888888), fontSize = 9.sp, fontWeight = FontWeight.SemiBold, maxLines = 1)
                                }
                            }
                        }

                        // Cart items
                        var priceEditItem by remember { mutableStateOf<CartItemEntity?>(null) }
                        var discountItem by remember { mutableStateOf<CartItemEntity?>(null) }
                        var marketPriceDetailItem by remember { mutableStateOf<CartItemEntity?>(null) }
                        var estimateItem by remember { mutableStateOf<CartItemEntity?>(null) }

                        if (priceEditItem != null) {
                            CartItemPriceDialog(
                                item = priceEditItem!!,
                                onConfirm = { newPrice ->
                                    viewModel.handleIntent(POSIntent.UpdateCartItemPrice(priceEditItem!!, newPrice))
                                    priceEditItem = null
                                },
                                onDismiss = { priceEditItem = null }
                            )
                        }

                        if (discountItem != null) {
                            CartItemDiscountDialog(
                                item = discountItem!!,
                                currentDiscountPct = state.cartItemDiscounts[discountItem!!.id] ?: 0.0,
                                onApply = { pct ->
                                    viewModel.handleIntent(POSIntent.ApplyItemDiscount(discountItem!!.id, pct))
                                    discountItem = null
                                },
                                onDismiss = { discountItem = null }
                            )
                        }

                        // Market price detail dialog
                        if (marketPriceDetailItem != null) {
                            val mpItem = marketPriceDetailItem!!
                            val mp = state.marketPrices[mpItem.productCode ?: mpItem.name]
                            if (mp != null) {
                                MarketPriceDetailDialog(
                                    item = mpItem,
                                    result = mp,
                                    storePrice = mpItem.price,
                                    onDismiss = { marketPriceDetailItem = null }
                                )
                            } else {
                                marketPriceDetailItem = null
                            }
                        }

                        if (estimateItem != null) {
                            val eItem = estimateItem!!
                            val eKey = eItem.productCode ?: eItem.name
                            val eMkt = state.marketPrices[eKey]
                            if (eMkt != null && eMkt.weighted_avg > 0) {
                                EstimateBreakdownDialog(
                                    item = eItem,
                                    result = eMkt,
                                    onDismiss = { estimateItem = null }
                                )
                            } else {
                                estimateItem = null
                            }
                        }

                        if (state.cartItems.isEmpty()) {
                            Box(
                                Modifier.weight(1f).fillMaxWidth(),
                                contentAlignment = Alignment.Center
                            ) {
                                val emptyFloat = rememberInfiniteTransition(label = "emptyCart")
                                val emptyY by emptyFloat.animateFloat(
                                    initialValue = 0f, targetValue = 8f,
                                    animationSpec = infiniteRepeatable(tween(2000, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ey"
                                )
                                val emptyAlpha by emptyFloat.animateFloat(
                                    initialValue = 0.3f, targetValue = 0.6f,
                                    animationSpec = infiniteRepeatable(tween(2000, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ea"
                                )
                                Column(
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                    modifier = Modifier.graphicsLayer(translationY = emptyY)
                                ) {
                                    Icon(Icons.Default.ShoppingCart, null, tint = Color(0xFF2A2A2A).copy(alpha = emptyAlpha), modifier = Modifier.size(48.dp))
                                    Spacer(Modifier.height(8.dp))
                                    Text("Cart is empty", color = Color(0xFF444444).copy(alpha = emptyAlpha), fontSize = 13.sp)
                                }
                            }
                        } else {
                            LazyColumn(
                                Modifier.weight(1f),
                                contentPadding = PaddingValues(vertical = 8.dp)
                            ) {
                                items(state.cartItems, key = { it.id }) { item ->
                                    val dismissState = rememberSwipeToDismissBoxState(
                                        confirmValueChange = { value ->
                                            if (value == SwipeToDismissBoxValue.EndToStart) {
                                                viewModel.handleIntent(POSIntent.RemoveFromCart(item))
                                                true
                                            } else false
                                        }
                                    )
                                    SwipeToDismissBox(
                                        state = dismissState,
                                        enableDismissFromStartToEnd = false,
                                        backgroundContent = {
                                            Box(
                                                Modifier.fillMaxSize().padding(horizontal = 20.dp),
                                                contentAlignment = Alignment.CenterEnd
                                            ) {
                                                Icon(Icons.Default.Delete, null, tint = Color(0xFFE53935), modifier = Modifier.size(20.dp))
                                            }
                                        }
                                    ) {
                                        if (item.productCode == "TRADE_IN_CREDIT") {
                                            // ── Trade-in credit row — special compact display ──
                                            Row(
                                                Modifier.fillMaxWidth().background(Color(0xFF0A1A0A))
                                                    .padding(horizontal = 20.dp, vertical = 12.dp),
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                Icon(Icons.Default.SwapHoriz, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(18.dp))
                                                Spacer(Modifier.width(10.dp))
                                                Column(Modifier.weight(1f)) {
                                                    Text(item.name, color = Color(0xFF4ADE80), fontSize = 13.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                                                    Text("Store credit applied", color = Color(0xFF4ADE80).copy(alpha = 0.5f), fontSize = 10.sp)
                                                }
                                                Text("−$${"%,.2f".format(-item.price)}", color = Color(0xFF4ADE80), fontSize = 15.sp, fontWeight = FontWeight.Black)
                                                Spacer(Modifier.width(8.dp))
                                                IconButton(onClick = { viewModel.handleIntent(POSIntent.RemoveFromCart(item)) }, modifier = Modifier.size(28.dp)) {
                                                    Icon(Icons.Default.Close, null, tint = Color(0xFF555555), modifier = Modifier.size(14.dp))
                                                }
                                            }
                                        } else {
                                        // Auto-trigger market price fetch when item first appears in cart
                                        val itemKey = item.productCode ?: item.name
                                        LaunchedEffect(itemKey) {
                                            viewModel.handleIntent(
                                                POSIntent.FetchMarketPrice(
                                                    qrCode = itemKey,
                                                    name = item.name,
                                                    setCode = "",
                                                    storePrice = item.price
                                                )
                                            )
                                        }
                                        CartItemRow(
                                            item = item,
                                            onIncrement = { viewModel.handleIntent(POSIntent.UpdateCartQuantity(item, 1)) },
                                            onDecrement = { viewModel.handleIntent(POSIntent.UpdateCartQuantity(item, -1)) },
                                            onPriceEdit = { priceEditItem = item },
                                            condition = state.cardConditions[item.id] ?: "NM",
                                            onConditionCycle = {
                                                val next = cycleCondition(state.cardConditions[item.id] ?: "NM")
                                                viewModel.handleIntent(POSIntent.SetCardCondition(item.id, next))
                                            },
                                            discountPct = state.cartItemDiscounts[item.id] ?: 0.0,
                                            onDiscount = { discountItem = item },
                                            marketPrice = state.marketPrices[itemKey],
                                            isFetchingMarket = state.marketPriceFetching.contains(itemKey),
                                            onMarketPriceTap = { marketPriceDetailItem = item },
                                            onHaggle = {
                                                val mkt = state.marketPrices[itemKey]?.weighted_avg ?: item.price
                                                viewModel.handleIntent(POSIntent.RequestHaggle(
                                                    itemId = item.id,
                                                    name = item.name,
                                                    storePrice = item.price,
                                                    marketPrice = mkt,
                                                    condition = state.cardConditions[item.id] ?: "NM"
                                                ))
                                            },
                                            onEstimate = { estimateItem = item }
                                        )
                                        } // end else (non-credit item)
                                    }
                                }
                            }
                        }

                        // Totals + checkout
                        Surface(color = Color(0xFF1A1A1A)) {
                            Column(Modifier.padding(20.dp)) {
                                TotalRow("Subtotal", state.subtotal)
                                Spacer(Modifier.height(4.dp))
                                TotalRow("Tax (5.6%)", state.taxAmount)
                                if (state.tipAmount > 0) {
                                    Spacer(Modifier.height(4.dp))
                                    TotalRow("Tip", state.tipAmount)
                                }
                                HorizontalDivider(color = Color(0xFF2A2A2A), modifier = Modifier.padding(vertical = 12.dp))
                                val cartTotalPulse = rememberInfiniteTransition(label = "cartTotalPulse")
                                val cartTotalGlow by cartTotalPulse.animateFloat(
                                    initialValue = 0.8f, targetValue = 1f,
                                    animationSpec = infiniteRepeatable(tween(1200, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ctg"
                                )
                                val chargeScale by cartTotalPulse.animateFloat(
                                    initialValue = 1f, targetValue = if (state.cartItems.isNotEmpty()) 1.03f else 1f,
                                    animationSpec = infiniteRepeatable(tween(900, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "cs"
                                )
                                val chargeBorderAlpha by cartTotalPulse.animateFloat(
                                    initialValue = 0f, targetValue = if (state.cartItems.isNotEmpty()) 0.6f else 0f,
                                    animationSpec = infiniteRepeatable(tween(900, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "cba"
                                )
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                    Text("TOTAL", fontWeight = FontWeight.Black, color = Color.White, fontSize = 14.sp, letterSpacing = 1.sp)
                                    Text(
                                        "$${String.format("%.2f", state.totalAmount)}",
                                        fontWeight = FontWeight.Black,
                                        color = Gold.copy(alpha = cartTotalGlow),
                                        fontSize = 26.sp
                                    )
                                }
                                if (state.cartItems.size >= 3) {
                                    LaunchedEffect(state.cartItems.size) {
                                        if (state.bundleDeal == null && !state.isBundleLoading) {
                                            viewModel.handleIntent(POSIntent.RequestBundleDeal)
                                        }
                                    }
                                    Spacer(Modifier.height(8.dp))
                                    BundleDealBanner(
                                        bundleDeal = state.bundleDeal,
                                        isLoading = state.isBundleLoading,
                                        onRequest = { viewModel.handleIntent(POSIntent.RequestBundleDeal) },
                                        onDismiss = { viewModel.handleIntent(POSIntent.DismissBundleDeal) }
                                    )
                                }
                                Spacer(Modifier.height(16.dp))
                                Button(
                                    onClick = {
                                        if (state.cartItems.isNotEmpty()) {
                                            showCheckout = true
                                            viewModel.pushKioskCheckoutStarted()
                                        }
                                    },
                                    enabled = state.cartItems.isNotEmpty() && !state.isProcessingPayment,
                                    modifier = Modifier.fillMaxWidth().height(60.dp)
                                        .graphicsLayer(scaleX = chargeScale, scaleY = chargeScale)
                                        .then(if (state.cartItems.isNotEmpty()) Modifier.border(1.5.dp, Gold.copy(alpha = chargeBorderAlpha), RoundedCornerShape(12.dp)) else Modifier),
                                    shape = RoundedCornerShape(12.dp),
                                    colors = ButtonDefaults.buttonColors(
                                        containerColor = Gold,
                                        contentColor = VaultBlack,
                                        disabledContainerColor = Color(0xFF2A2A2A),
                                        disabledContentColor = Color(0xFF555555)
                                    )
                                ) {
                                    if (state.isProcessingPayment) {
                                        CircularProgressIndicator(modifier = Modifier.size(20.dp), color = VaultBlack, strokeWidth = 2.dp)
                                    } else {
                                        Text("CHARGE  $${String.format("%.2f", state.totalAmount)}", fontWeight = FontWeight.Black, fontSize = 16.sp)
                                    }
                                }
                            }
                        }
                    }

                    var scanFlashKey by remember { mutableIntStateOf(0) }
                    LaunchedEffect(cartSize) { if (cartSize > 0) scanFlashKey++ }
                    if (scanFlashKey > 0) {
                        key(scanFlashKey) {
                            val flashAlpha = remember { Animatable(0.25f) }
                            LaunchedEffect(Unit) { flashAlpha.animateTo(0f, tween(400, easing = FastOutSlowInEasing)) }
                            Box(
                                Modifier.fillMaxSize()
                                    .graphicsLayer(alpha = flashAlpha.value)
                                    .background(
                                        Brush.verticalGradient(
                                            listOf(Gold.copy(alpha = 0.3f), Color.Transparent),
                                            startY = 0f, endY = 300f
                                        )
                                    )
                            )
                        }
                    }
                    } // end cart Box
                }
            }

            // Checkout overlay
            if (showCheckout) {
                CheckoutModal(
                    state = state,
                    paymentLauncher = paymentLauncher,
                    venmoPaymentLauncher = venmoPaymentLauncher,
                    onDismiss = { showCheckout = false },
                    onZettleLaunched = { pendingPrintReceipt = it },
                    onDeductCustomerCredit = { custId, amount ->
                        viewModel.handleIntent(POSIntent.DeductCustomerCredit(custId, amount))
                    },
                    onCardPayment = { amountCents, reference, total, print ->
                        viewModel.triggerCardPayment(amountCents, reference, total, print)
                    },
                    onSetCustomerPhone = { phone ->
                        viewModel.handleIntent(POSIntent.SetCustomerPhone(phone))
                    },
                    onComplete = { method, received, change, shouldPrint ->
                        viewModel.handleIntent(POSIntent.CompleteSale(method, received, change, shouldPrint = shouldPrint))
                        showCheckout = false
                        showSuccess = true
                    }
                )
            }

            // Success overlay
            if (showSuccess) {
                SuccessScreen { showSuccess = false }
            }

            // Card Declined overlay
            if (showCardDeclined) {
                CardDeclinedDialog(onDismiss = { showCardDeclined = false })
            }

            // Haggle Assistant dialog
            if (state.haggleResult != null || state.isHaggleLoading) {
                HaggleAssistantDialog(
                    result = state.haggleResult,
                    isLoading = state.isHaggleLoading,
                    itemName = state.cartItems.find { it.id == state.haggleItemId }?.name ?: "",
                    onDismiss = { viewModel.handleIntent(POSIntent.DismissHaggle) }
                )
            }

            // Voice input overlay
            if (showVoiceInput) {
                VoiceInputDialog(
                    onResult = { text ->
                        showVoiceInput = false
                        if (text.isNotBlank()) {
                            viewModel.handleIntent(POSIntent.SearchInventory(text))
                        }
                    },
                    onDismiss = { showVoiceInput = false }
                )
            }

            // Counterfeit scanner
            if (showCounterfeitCamera) {
                CounterfeitScannerDialog(
                    result = state.counterfeitResult,
                    isLoading = state.isCounterfeitLoading,
                    onCapture = { imageB64, cardName ->
                        viewModel.handleIntent(POSIntent.CheckCounterfeit(imageB64, cardName))
                    },
                    onDismiss = {
                        showCounterfeitCamera = false
                        viewModel.handleIntent(POSIntent.DismissCounterfeit)
                    }
                )
            }

            // OCR + Visual + Smart card scan
            if (state.showOcrScanDialog) {
                OcrScanDialog(
                    state = state,
                    onCapture = { b64 -> viewModel.handleIntent(POSIntent.ScanCardOcr(b64)) },
                    onCaptureVisual = { b64 -> viewModel.handleIntent(POSIntent.ScanCardVisual(b64)) },
                    onCaptureSmart = { b64 -> viewModel.handleIntent(POSIntent.ScanCardSmart(b64)) },
                    onFetchWorldPrice = { q -> viewModel.handleIntent(POSIntent.FetchWorldPrice(q)) },
                    onFetchPriceV2 = { req -> viewModel.handleIntent(POSIntent.FetchPriceV2(req)) },
                    onLogPick = { cands, idx, action ->
                        viewModel.handleIntent(POSIntent.LogPick(cands, idx, action))
                    },
                    onAddToCart = { name, price ->
                        viewModel.handleIntent(POSIntent.AddProduct(name, price, "SCANNED", ""))
                        viewModel.handleIntent(POSIntent.DismissOcrScanDialog)
                    },
                    onDismiss = { viewModel.handleIntent(POSIntent.DismissOcrScanDialog) }
                )
            }

            // Variant picker — long-press a product card to pick exact printing
            if (state.variantPickerProduct != null) {
                VariantPickerSheet(
                    product = state.variantPickerProduct!!,
                    variants = state.variantPickerCards,
                    isLoading = state.isLoadingVariantPicker,
                    onSelect = { variant -> viewModel.handleIntent(POSIntent.AddVariantToCart(variant)) },
                    onQuickAdd = { viewModel.handleIntent(POSIntent.AddToCart(state.variantPickerProduct!!)) },
                    onDismiss = { viewModel.handleIntent(POSIntent.DismissVariantPicker) }
                )
            }

            // Sales History panel
            if (showSalesHistory) {
                SalesHistoryPanel(
                    sales = state.recentSales,
                    consignors = state.consignors,
                    consignmentItems = state.consignmentItems,
                    onRefund = { saleId ->
                        viewModel.handleIntent(POSIntent.RefundSale(saleId))
                    },
                    onIntent = { viewModel.handleIntent(it) },
                    onDismiss = { showSalesHistory = false }
                )
            }

            // Customers / Store Credit panel
            if (showCustomers) {
                CustomerManagementPanel(
                    customers = state.customers,
                    onAddCredit = { custId, amount -> viewModel.handleIntent(POSIntent.AddCustomerCredit(custId, amount)) },
                    onCreateCustomer = { name, credit -> viewModel.handleIntent(POSIntent.CreateCustomer(name, credit)) },
                    onDeleteCustomer = { custId -> viewModel.handleIntent(POSIntent.DeleteCustomer(custId)) },
                    onDismiss = { showCustomers = false },
                    onCheckout = { showCustomers = false; showCheckout = true; viewModel.pushKioskCheckoutStarted() }
                )
            }

            // Reprice Queue sheet
            if (showRepriceQueue) {
                RepricingQueueSheet(
                    queue = state.repriceQueue,
                    onDismiss = { showRepriceQueue = false },
                    onApply = { qrCode, newPrice -> viewModel.handleIntent(POSIntent.ApplyRepriceSuggestion(qrCode, newPrice)) },
                    onDismissSuggestion = { qrCode -> viewModel.handleIntent(POSIntent.DismissRepriceSuggestion(qrCode)) },
                    onApplyAll = { viewModel.handleIntent(POSIntent.ApplyAllRepriceSuggestions) }
                )
            }

            // Trade-In sheet
            if ((showTradeIn || state.isTradeInMode) && !showCustomerSigning) {
                TradeInSheet(
                    state = state,
                    onDismiss = {
                        // ALWAYS clear isTradeInMode on dismiss — otherwise the sheet auto-reopens
                        // (because the show condition is `(showTradeIn || isTradeInMode) && !showCustomerSigning`).
                        showTradeIn = false
                        viewModel.handleIntent(POSIntent.HideTradeInSheet)
                        if (state.tradeInItems.isEmpty()) viewModel.handleIntent(POSIntent.ClearTradeIn)
                    },
                    onRemoveItem = { qrCode -> viewModel.handleIntent(POSIntent.RemoveTradeInItem(qrCode)) },
                    onCancel = { viewModel.handleIntent(POSIntent.ClearTradeIn); showTradeIn = false },
                    onFinalize = { name, type -> viewModel.handleIntent(POSIntent.FinalizeTradeIn(name, type)); showTradeIn = false },
                    onApplyToCart = { name -> viewModel.handleIntent(POSIntent.ApplyTradeInToCart(name)) },
                    onCheckout = { showTradeIn = false; showCheckout = true; viewModel.pushKioskCheckoutStarted() },
                    onSetManualCredit = { amt -> viewModel.handleIntent(POSIntent.SetWidgetTradeCredit(amt)) },
                    onManualAdd = { name, mktPrice -> viewModel.handleIntent(POSIntent.ManualTradeInAdd(name, mktPrice)) },
                    onSearchMarketPrice = { q -> viewModel.handleIntent(POSIntent.SearchMarketPrice(q, "", "")) },
                    onAddToCart = { name, price ->
                        val qr = "TRADE_CART_${System.currentTimeMillis()}"
                        val product = ProductEntity(qrCode = qr, name = name, price = price, category = "Pokemon", setCode = "", stockQuantity = 1)
                        viewModel.handleIntent(POSIntent.AddToCart(product))
                    },
                    onCashOut = { name, price ->
                        val qr = "TRADE_CART_${System.currentTimeMillis()}"
                        val product = ProductEntity(qrCode = qr, name = name, price = price, category = "Pokemon", setCode = "", stockQuantity = 1)
                        viewModel.handleIntent(POSIntent.AddToCart(product))
                        showTradeIn = false
                        showCheckout = true
                    },
                    onRequestSigning = { items, action ->
                        // items are passed explicitly from the button click — no race possible
                        Log.i("POS", "🔵 onRequestSigning fired — items=${items.size}")
                        signingItems = items
                        pendingSigningAction = action
                        showTradeIn = false
                        viewModel.handleIntent(POSIntent.HideTradeInSheet)
                        showCustomerSigning = true
                    },
                    onUpdateOffer = { qrCode, newBuy, newCredit ->
                        viewModel.handleIntent(POSIntent.UpdateTradeItemOffer(qrCode, newBuy, newCredit))
                    },
                    onScanOutgoing = { qr -> viewModel.handleIntent(POSIntent.AddTradeOutItemByQr(qr)) },
                    onRemoveOutgoing = { qr -> viewModel.handleIntent(POSIntent.RemoveTradeOutItem(qr)) },
                    onUpdateOutgoingPrice = { qr, p -> viewModel.handleIntent(POSIntent.UpdateTradeOutItemPrice(qr, p)) },
                    onRestoreCanceled = { snapId -> viewModel.handleIntent(POSIntent.RestoreCanceledTrade(snapId)) },
                    onDismissCanceledBanner = { viewModel.handleIntent(POSIntent.DismissCanceledBanner) }
                )
            }

            // ── Tablet Incoming Offer Sheet (admin-pushed trade-in) ────────────────
            if (showTabletOffer && state.customerOffer != null && !state.customerOffer.empty) {
                val currentOffer = state.customerOffer
                TabletIncomingOfferSheet(
                    offer = currentOffer,
                    offerStatus = state.customerOfferStatus,
                    onAcceptAndSign = { modifiedItems, modifiedCash, modifiedCredit ->
                        // Build the signing items list LOCALLY from what the customer
                        // ACTUALLY agreed to (modifiedItems may have removed cards or
                        // reduced offers). This is what gets printed on the receipt.
                        val ratio = if (modifiedCash > 0.0) modifiedCredit / modifiedCash else 1.20
                        val snapshot = modifiedItems.map { item ->
                            TradeInItem(
                                product = ProductEntity(
                                    qrCode = item.qr_code.ifEmpty { "OFFER_${item.id}" },
                                    name = item.name,
                                    price = item.market,
                                    category = "Pokemon",
                                    setCode = "",
                                    stockQuantity = 1
                                ),
                                marketPrice = item.market,
                                buyOffer = item.offer,
                                tradeCredit = item.offer * ratio
                            )
                        }
                        signingItems = snapshot
                        viewModel.handleIntent(POSIntent.TabletAcceptOffer(currentOffer.ti_id))
                        viewModel.handleIntent(POSIntent.LoadOfferAsTradeIn)
                        showTabletOffer = false
                        showCustomerSigning = true
                        val tiId = currentOffer.ti_id
                        val customerName = currentOffer.customer
                        pendingSigningAction = {
                            viewModel.handleIntent(POSIntent.FinalizeTradeIn(customerName, TradeOfferType.CASH))
                            viewModel.handleIntent(POSIntent.DismissTabletOffer)
                            Log.i("POS", "Tablet offer ti_id=$tiId accepted (${modifiedItems.size}/${currentOffer.items.size} cards, $modifiedCash cash), signed, finalized")
                        }
                    },
                    onDecline = {
                        viewModel.handleIntent(POSIntent.TabletRejectOffer(currentOffer.ti_id))
                        showTabletOffer = false
                        viewModel.handleIntent(POSIntent.DismissTabletOffer)
                    },
                    onDismiss = {
                        // Just hide locally — the offer stays in state so the sidebar pill
                        // can reopen it. Do NOT call DismissTabletOffer here.
                        showTabletOffer = false
                    },
                    onModify = { modItems, modCash, modCredit ->
                        // Best-effort sync to the server so the admin POS sees what the
                        // customer is editing in real-time. Failure is non-fatal — the
                        // local state still flows into the signed receipt.
                        viewModel.handleIntent(POSIntent.TabletModifyOffer(currentOffer.ti_id, modItems, modCash, modCredit))
                    }
                )
            }

            // ── Customer Signing Screen overlay ────────────────────────────────────────
            // zIndex(1000f) forces this overlay to render ABOVE every other conditional
            // sheet (lot-eval / arbitrage / market-search / etc.) regardless of source order.
            if (showCustomerSigning) {
                LaunchedEffect(Unit) { Log.i("POS", "🟢 CustomerSigningScreen rendering — items=${signingItems.size}, decision=$signingDecision") }
                Box(modifier = Modifier.fillMaxSize().zIndex(1000f)) {
                CustomerSigningScreen(
                    // Use snapshot captured at signing-start; fall back to live state
                    // for the tablet-offer path where items load asynchronously.
                    items = signingItems.ifEmpty { state.tradeInItems },
                    disclosure = state.customerDisplayDisclosure,
                    decision = signingDecision,
                    onAccept = { png, name ->
                        signingDecision = "accepted"
                        viewModel.handleIntent(POSIntent.PrintSignedTradeSlip(signaturePng = png, customerName = name))
                    },
                    onReject = {
                        signingDecision = "rejected"
                    },
                    onDecisionComplete = {
                        showCustomerSigning = false
                        signingDecision = ""
                        signingItems = emptyList()
                        pendingSigningAction?.invoke()
                        pendingSigningAction = null
                    },
                    onBackToAdmin = {
                        // Reset EVERY signing flag so a future trade is not blocked by stale state.
                        showCustomerSigning = false
                        signingItems = emptyList()
                        signingDecision = ""
                        pendingSigningAction = null
                    },
                    onUpdateDisclosure = { text ->
                        viewModel.handleIntent(POSIntent.UpdateCustomerDisplayDisclosure(text))
                    }
                )
                }
            }

            // Lot Speed Evaluator sheet
            if (showLotEval || state.isLotEvalMode) {
                LotEvaluatorSheet(
                    items = state.lotEvalItems,
                    isActive = state.isLotEvalMode,
                    onDismiss = { showLotEval = false },
                    onToggleMode = { viewModel.handleIntent(POSIntent.ToggleLotEvalMode) },
                    onRemoveItem = { qrCode -> viewModel.handleIntent(POSIntent.RemoveLotEvalItem(qrCode)) },
                    onClear = { viewModel.handleIntent(POSIntent.ClearLotEval); showLotEval = false },
                    onConvertToTradeIn = {
                        viewModel.handleIntent(POSIntent.ConvertLotToTradeIn)
                        showLotEval = false
                        showTradeIn = true
                    }
                )
            }

            // Arbitrage Scout sheet
            if (showArbitrageScout) {
                ArbitrageScoutSheet(
                    repriceQueue = state.repriceQueue,
                    onDismiss = { showArbitrageScout = false },
                    onUpdatePrice = { qrCode, price ->
                        viewModel.handleIntent(POSIntent.ApplyRepriceSuggestion(qrCode, price))
                    },
                    onUpdateAll = {
                        val underpriced = state.repriceQueue.filter { it.pctChange > 0 }
                        underpriced.forEach { s ->
                            viewModel.handleIntent(POSIntent.ApplyRepriceSuggestion(s.product.qrCode, s.suggestedPrice))
                        }
                        showArbitrageScout = false
                    }
                )
            }

            // Market Price Search sheet
            if (showMarketSearch) {
                MarketPriceSearchSheet(
                    state = state,
                    onDismiss = { showMarketSearch = false },
                    onSearch = { query, setCode, cardNumber ->
                        viewModel.handleIntent(POSIntent.SearchMarketPrice(query, setCode, cardNumber))
                    },
                    onLoadVariants = { q -> viewModel.handleIntent(POSIntent.LoadMarketVariants(q)) },
                    onSelectVariant = { idx -> viewModel.handleIntent(POSIntent.SelectMarketVariant(idx)) },
                    onSetLanguage = { lang -> viewModel.handleIntent(POSIntent.SetMarketLanguage(lang)) },
                    onClear = { viewModel.handleIntent(POSIntent.ClearMarketSearch) },
                    cacheSize = state.marketSearchCache.size
                )
            }

            // EOD confirmation dialog
            if (showEodConfirm) {
                val scope = rememberCoroutineScope()
                val eodContext = androidx.compose.ui.platform.LocalContext.current
                var cashClosingEntry by remember { mutableStateOf("") }
                AlertDialog(
                    onDismissRequest = { showEodConfirm = false },
                    containerColor = Color(0xFF1A1A1A),
                    tonalElevation = 0.dp,
                    shape = RoundedCornerShape(16.dp),
                    title = {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.Assessment, null, tint = Gold, modifier = Modifier.size(20.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("End of Day", color = Color.White, fontWeight = FontWeight.Bold)
                        }
                    },
                    text = {
                        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Text(
                                "Generates today's Z-Report. Opening float: $${String.format("%.2f", state.openingFloat)}",
                                color = Color(0xFFCCCCCC),
                                fontSize = 13.sp
                            )
                            Text("Cash drawer count (optional)", color = Color(0xFF888888), fontSize = 12.sp)
                            OutlinedTextField(
                                value = cashClosingEntry,
                                onValueChange = { v ->
                                    val cleaned = v.filter { it.isDigit() || it == '.' }
                                    cashClosingEntry = cleaned
                                },
                                placeholder = { Text("0.00", color = Color(0xFF444444)) },
                                prefix = { Text("$", color = Color(0xFF888888)) },
                                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                singleLine = true,
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Gold,
                                    unfocusedBorderColor = Color(0xFF444444),
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White
                                ),
                                modifier = Modifier.fillMaxWidth()
                            )
                        }
                    },
                    confirmButton = {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            // Z-Report print is now available in BOTH PI mode
                            // (via /print/receipt as a synthetic receipt) and
                            // when a local printer is configured. The button
                            // is hidden only if the printer mode is something
                            // other than PI AND no local printer is set up.
                            val canPrintZReport = state.printerMode == "PI" || PrinterManager.isConfigured()
                            if (canPrintZReport) {
                                Button(
                                    onClick = {
                                        showEodConfirm = false
                                        scope.launch {
                                            val startOfDay = run {
                                                val cal = java.util.Calendar.getInstance()
                                                cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
                                                cal.set(java.util.Calendar.MINUTE, 0)
                                                cal.set(java.util.Calendar.SECOND, 0)
                                                cal.set(java.util.Calendar.MILLISECOND, 0)
                                                cal.timeInMillis
                                            }
                                            val sales = withContext(kotlinx.coroutines.Dispatchers.IO) {
                                                viewModel.getSalesSinceDay(startOfDay)
                                            }
                                            val dateStr = java.text.SimpleDateFormat("MMM dd yyyy  h:mm a", java.util.Locale.US).format(java.util.Date())
                                            val closingCount = cashClosingEntry.toDoubleOrNull() ?: 0.0
                                            if (state.printerMode == "PI") {
                                                viewModel.printZReportToPi(
                                                    date = dateStr,
                                                    sales = sales,
                                                    openingFloat = state.openingFloat,
                                                    closingCount = closingCount
                                                )
                                            } else {
                                                val receiptCfg = ReceiptConfigPreference.get(eodContext)
                                                withContext(kotlinx.coroutines.Dispatchers.IO) {
                                                    PrinterManager.printZReport(
                                                        date = dateStr,
                                                        sales = sales,
                                                        openingFloat = state.openingFloat,
                                                        closingCount = closingCount,
                                                        config = receiptCfg
                                                    )
                                                }
                                            }
                                        }
                                    },
                                    colors = ButtonDefaults.buttonColors(containerColor = Gold),
                                    modifier = Modifier.fillMaxWidth()
                                ) {
                                    Icon(Icons.Default.Print, null, tint = VaultBlack, modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(6.dp))
                                    Text(
                                        if (state.printerMode == "PI") "Print Z-Report (Pi)" else "Print Z-Report",
                                        color = VaultBlack, fontWeight = FontWeight.Bold
                                    )
                                }
                            }
                            Button(
                                onClick = {
                                    showEodConfirm = false
                                    scope.launch {
                                        val startOfDay = run {
                                            val cal = java.util.Calendar.getInstance()
                                            cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
                                            cal.set(java.util.Calendar.MINUTE, 0)
                                            cal.set(java.util.Calendar.SECOND, 0)
                                            cal.set(java.util.Calendar.MILLISECOND, 0)
                                            cal.timeInMillis
                                        }
                                        val sales = withContext(kotlinx.coroutines.Dispatchers.IO) {
                                            viewModel.getSalesSinceDay(startOfDay)
                                        }
                                        val closingCount = cashClosingEntry.toDoubleOrNull() ?: 0.0
                                        sendEodEmail(eodContext, sales, state.openingFloat, closingCount)
                                    }
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF7A0000)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Icon(Icons.Default.Email, null, tint = Color.White, modifier = Modifier.size(16.dp))
                                Spacer(Modifier.width(6.dp))
                                Text("Email Report", color = Color.White)
                            }
                        }
                    },
                    dismissButton = {
                        TextButton(onClick = { showEodConfirm = false }) {
                            Text("Cancel", color = Gold)
                        }
                    }
                )
            }

            // Snackbar overlay
            SnackbarHost(
                hostState = snackbarHostState,
                modifier = Modifier.align(Alignment.BottomCenter).padding(bottom = 16.dp)
            ) { data ->
                Snackbar(
                    snackbarData = data,
                    containerColor = VaultSurface,
                    contentColor = Color.White,
                    actionColor = Gold,
                    shape = RoundedCornerShape(12.dp)
                )
            }
        }
    }

    @Composable
    fun POSHeader(state: POSViewState, onSettingsClick: () -> Unit, onEodClick: () -> Unit = {}, onPrinterDotClick: () -> Unit = {}, @Suppress("UNUSED_PARAMETER") onRepriceClick: () -> Unit = {}, @Suppress("UNUSED_PARAMETER") onArbitrageClick: () -> Unit = {}, @Suppress("UNUSED_PARAMETER") onMarketSearchClick: () -> Unit = {}) {
        var timeStr by remember { mutableStateOf("") }
        LaunchedEffect(Unit) {
            val fmt = SimpleDateFormat("h:mm a", java.util.Locale.US)
            while (true) {
                timeStr = fmt.format(Date())
                delay(10_000)
            }
        }
        Row(
            Modifier.fillMaxWidth().height(56.dp).background(VaultGrey).padding(horizontal = 20.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("HANRYX VAULT", color = Gold, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 3.sp)
            AnimatedVisibility(
                visible = state.isSyncing,
                enter = fadeIn(tween(200)) + scaleIn(tween(300), initialScale = 0.5f),
                exit = fadeOut(tween(200)) + scaleOut(tween(200), targetScale = 0.5f)
            ) {
                Row {
                    Spacer(Modifier.width(10.dp))
                    val syncSpin = rememberInfiniteTransition(label = "syncSpin")
                    val syncAngle by syncSpin.animateFloat(
                        initialValue = 0f, targetValue = 360f,
                        animationSpec = infiniteRepeatable(tween(1200, easing = LinearEasing)), label = "sa"
                    )
                    CircularProgressIndicator(
                        modifier = Modifier.size(14.dp).graphicsLayer(rotationZ = syncAngle),
                        color = Gold, strokeWidth = 1.5.dp
                    )
                }
            }
            Spacer(Modifier.weight(1f))
            if (state.isRegisterMode) {
                Surface(
                    color = Color(0xFF3D1800),
                    shape = RoundedCornerShape(6.dp)
                ) {
                    Row(
                        Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(Icons.Default.Lock, null, tint = Color(0xFFFF6B00), modifier = Modifier.size(10.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("REGISTER LOCKED", color = Color(0xFFFF6B00), fontSize = 9.sp, fontWeight = FontWeight.Bold)
                    }
                }
                Spacer(Modifier.width(12.dp))
            }
            Text(timeStr, color = Color(0xFF888888), fontSize = 13.sp)
            Spacer(Modifier.width(16.dp))
            Text(state.employeeId ?: "---", color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium)
            Spacer(Modifier.width(12.dp))
            if (state.repriceQueue.isNotEmpty()) {
                val repriceTotal = state.repriceQueue.size
                Surface(color = Color(0xFF451A00), shape = RoundedCornerShape(6.dp)) {
                    Text(
                        "$repriceTotal",
                        color = Color(0xFFF59E0B),
                        fontSize = 9.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 3.dp)
                    )
                }
                Spacer(Modifier.width(6.dp))
            }
            // ── Pi printer status dot ─────────────────────────────────
            // Only shown in PI mode. Tap to force an immediate /print/status
            // probe (otherwise polled every 5 s).
            if (state.printerMode == "PI") {
                val dotColor = when (state.piPrinterReady) {
                    true  -> Color(0xFF22C55E) // green = ready
                    false -> Color(0xFFEF4444) // red   = unreachable / no printer
                    null  -> Color(0xFF888888) // grey  = checking
                }
                Surface(
                    onClick = onPrinterDotClick,
                    color = Color(0xFF1E1E1E),
                    shape = RoundedCornerShape(6.dp)
                ) {
                    Row(
                        Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Box(Modifier.size(8.dp).background(dotColor, CircleShape))
                        Spacer(Modifier.width(6.dp))
                        Text("Pi Printer", color = Color(0xFFCCCCCC), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                }
                Spacer(Modifier.width(8.dp))
            }
            Surface(
                onClick = onEodClick,
                color = Color(0xFF7A0000),
                shape = RoundedCornerShape(6.dp)
            ) {
                Text(
                    "EOD",
                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp),
                    color = Color.White,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    letterSpacing = 1.sp
                )
            }
            Spacer(Modifier.width(8.dp))
            IconButton(onClick = onSettingsClick, modifier = Modifier.size(40.dp)) {
                Icon(Icons.Default.Settings, null, tint = Color(0xFF666666), modifier = Modifier.size(22.dp))
            }
        }
        HorizontalDivider(color = Color(0xFF1E1E1E))
    }

    @Composable
    fun CategorySideItem(name: String, selected: Boolean, icon: androidx.compose.ui.graphics.vector.ImageVector, onClick: () -> Unit) {
        val bgAlpha by animateFloatAsState(if (selected) 0.12f else 0f, tween(250), label = "catBg")
        val accentWidth by animateDpAsState(if (selected) 3.dp else 0.dp, tween(200), label = "catAccent")
        val iconTint by animateColorAsState(if (selected) Gold else Color(0xFF555555), tween(250), label = "catIcon")
        val iconScale by animateFloatAsState(if (selected) 1.1f else 1f, spring(dampingRatio = 0.6f), label = "catScale")
        Box(
            Modifier
                .fillMaxWidth()
                .clickable { onClick() }
                .background(Gold.copy(alpha = bgAlpha))
                .padding(vertical = 10.dp),
            contentAlignment = Alignment.Center
        ) {
            Box(Modifier.width(accentWidth).height(32.dp).align(Alignment.CenterStart).background(Gold))
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(icon, null, tint = iconTint, modifier = Modifier.size(22.dp).graphicsLayer(scaleX = iconScale, scaleY = iconScale))
                Spacer(Modifier.height(3.dp))
                Text(name, fontSize = 10.sp, color = iconTint, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center)
            }
        }
    }

    // Legacy kept for any existing references
    @Composable
    fun CategoryItem(name: String, selected: Boolean, icon: androidx.compose.ui.graphics.vector.ImageVector, onClick: () -> Unit) {
        CategorySideItem(name, selected, icon, onClick)
    }

    @Composable
    fun ConnectionChip(label: String, active: Boolean) {
        Row(
            Modifier
                .clip(RoundedCornerShape(6.dp))
                .background(if (active) Gold.copy(alpha = 0.08f) else Color.Red.copy(alpha = 0.08f))
                .padding(horizontal = 10.dp, vertical = 5.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(5.dp).clip(CircleShape).background(if (active) Gold else Color.Red))
            Spacer(Modifier.width(6.dp))
            Text(label, fontSize = 9.sp, color = if (active) Gold else Color.Red, fontWeight = FontWeight.Bold)
        }
    }

    @Composable
    fun CartItemRowSlot(
        item: CartItemEntity,
        onIncrement: () -> Unit,
        onDecrement: () -> Unit,
        onPriceEdit: () -> Unit = {},
        condition: String = "NM",
        onConditionCycle: () -> Unit = {},
        discountPct: Double = 0.0,
        onDiscount: () -> Unit = {},
        marketPrice: MarketPriceResult? = null,
        isFetchingMarket: Boolean = false,
        onMarketPriceTap: () -> Unit = {},
        onHaggle: () -> Unit = {},
        onEstimate: () -> Unit = {}
    ) = CartItemRow(item, onIncrement, onDecrement, onPriceEdit, condition, onConditionCycle, discountPct, onDiscount, marketPrice, isFetchingMarket, onMarketPriceTap, onHaggle, onEstimate)

    @Composable
    fun TotalRow(label: String, amount: Double) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(label, color = Color(0xFF888888), fontSize = 12.sp)
            Text("$${String.format("%.2f", amount)}", color = Color(0xFF888888), fontSize = 12.sp)
        }
    }

    @Composable
    fun ProductCardSlot(product: ProductEntity, onClick: () -> Unit) =
        ProductCard(product, onClick)

    override fun onResume() {
        super.onResume()
        viewModel.handleIntent(POSIntent.RefreshZettleAuth)
        viewModel.updateSdkLoginState()
    }

    private fun setupZettleAuthObserver() {
        val liveData = ZettleSDK.instance?.authState ?: run {
            Log.w("VaultAuth", "ZettleSDK.instance is null — cannot observe authState")
            return
        }
        val observer = Observer<User.AuthState> { authState ->
            Log.d("VaultAuth", "Zettle authState changed: $authState")
            viewModel.handleIntent(POSIntent.RefreshZettleAuth)
            // Also update the on-device SDK login state immediately.
            // Use a short delay so the SDK has time to persist the tokens after the auth flow.
            lifecycleScope.launch {
                delay(400)
                viewModel.updateSdkLoginState()
            }
        }
        liveData.observeForever(observer)
        zettleAuthObserver = observer
        Log.d("VaultAuth", "Registered Zettle authState observer")
    }

    private val bluetoothPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        val denied = results.filter { !it.value }.keys.map { it.substringAfterLast('.') }
        if (denied.isEmpty()) {
            Log.d("VaultBT", "All Bluetooth permissions granted — Zettle BLE scanner ready")
        } else {
            Log.w("VaultBT", "Bluetooth permissions denied: $denied — card reader may not be discoverable")
            Toast.makeText(this, "Bluetooth permissions needed for card reader. Grant them in Settings → Apps → HanryxVault → Permissions.", Toast.LENGTH_LONG).show()
        }
    }

    private fun requestBluetoothPermissions() {
        val perms = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(
                android.Manifest.permission.BLUETOOTH_SCAN,
                android.Manifest.permission.BLUETOOTH_CONNECT,
                android.Manifest.permission.ACCESS_FINE_LOCATION,
                android.Manifest.permission.CAMERA
            )
        } else {
            arrayOf(
                android.Manifest.permission.ACCESS_FINE_LOCATION,
                android.Manifest.permission.CAMERA
            )
        }
        val needed = perms.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            bluetoothPermissionLauncher.launch(needed.toTypedArray())
            Log.d("VaultBT", "Requesting Bluetooth permissions: $needed")
        } else {
            Log.d("VaultBT", "All Bluetooth permissions already granted")
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        zettleAuthObserver?.let { ZettleSDK.instance?.authState?.removeObserver(it) }
        zettleAuthObserver = null
    }
}

@Composable fun CheckoutModal(state: POSViewState, paymentLauncher: ActivityResultLauncher<Intent>, venmoPaymentLauncher: ActivityResultLauncher<Intent>, onDismiss: () -> Unit, onZettleLaunched: (shouldPrint: Boolean) -> Unit = {}, onDeductCustomerCredit: (Int, Double) -> Unit = { _, _ -> }, onCardPayment: (amountCents: Long, reference: String, total: Double, printReceipt: Boolean) -> Unit = { _, _, _, _ -> }, onSetCustomerPhone: (String) -> Unit = {}, onComplete: (PaymentMethod, Double, Double, Boolean) -> Unit) {
    val context = LocalContext.current
    var selectedTip by remember { mutableStateOf(0.0) }
    var cashEntry by remember { mutableStateOf("") }
    var showCashPad by remember { mutableStateOf(false) }
    var manualCardMode by remember { mutableStateOf(false) }
    var printReceipt by remember { mutableStateOf(true) }
    var textReceipt by remember { mutableStateOf(false) }
    var customerPhone by remember { mutableStateOf("") }
    LaunchedEffect(customerPhone) { onSetCustomerPhone(customerPhone) }
    var showPayPalDialog by remember { mutableStateOf(false) }
    var payPalOrderId by remember { mutableStateOf("") }
    var payPalApproveUrl by remember { mutableStateOf("") }
    var payPalLoading by remember { mutableStateOf(false) }
    var payPalError by remember { mutableStateOf("") }
    var payPalPaid by remember { mutableStateOf(false) }
    val payPalScope = rememberCoroutineScope()
    var showCashAppDialog by remember { mutableStateOf(false) }

    val tipPercents = listOf(0, 10, 15, 18, 20)
    val total = state.totalAmount + selectedTip
    val cashReceived = cashEntry.toDoubleOrNull() ?: 0.0
    val changeDue = (cashReceived - total).coerceAtLeast(0.0)

    val tradeCredits = state.cartItems.filter { it.productCode == "TRADE_IN_CREDIT" }
    val creditApplied = tradeCredits.sumOf { it.price * it.quantity }
    val productsSubtotal = state.cartItems.filter { it.productCode != "TRADE_IN_CREDIT" }.sumOf { it.price * it.quantity }
    val itemCount = state.cartItems.filter { it.productCode != "TRADE_IN_CREDIT" }.sumOf { it.quantity }

    fun appendDigit(d: String) {
        val cur = cashEntry
        if (d == "." && cur.contains(".")) return
        if (d == "." && cur.isEmpty()) { cashEntry = "0."; return }
        val newVal = cur + d
        if (newVal.endsWith(".") || newVal.toDoubleOrNull() != null) cashEntry = newVal
    }
    fun backspace() { if (cashEntry.isNotEmpty()) cashEntry = cashEntry.dropLast(1) }

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.85f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(580.dp).wrapContentHeight().heightIn(max = 780.dp).clickable(onClick = {}),
            color = Color(0xFF131313),
            shape = RoundedCornerShape(20.dp),
            tonalElevation = 8.dp
        ) {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Surface(
                    Modifier.fillMaxWidth(),
                    color = Color(0xFF0D0D0D),
                    shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
                ) {
                    Column(Modifier.padding(horizontal = 24.dp, vertical = 16.dp)) {
                        val headerPulse = rememberInfiniteTransition(label = "headerPulse")
                        val headerGlow by headerPulse.animateFloat(
                            initialValue = 0.75f, targetValue = 1f,
                            animationSpec = infiniteRepeatable(tween(1500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "hg"
                        )
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("CHECKOUT", color = Gold.copy(alpha = headerGlow), fontWeight = FontWeight.Black, fontSize = 18.sp, letterSpacing = 3.sp)
                                Spacer(Modifier.width(10.dp))
                                Surface(color = Color(0xFF1A1A1A), shape = RoundedCornerShape(4.dp)) {
                                    Text("$itemCount item${if (itemCount != 1) "s" else ""}", color = Color(0xFF888888), fontSize = 10.sp, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp))
                                }
                            }
                            IconButton(onClick = onDismiss) { Icon(Icons.Default.Close, null, tint = Color.Gray) }
                        }
                        Spacer(Modifier.height(12.dp))
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Bottom) {
                            Column(Modifier.weight(1f)) {
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                    Text("Subtotal", color = Color(0xFF888888), fontSize = 12.sp)
                                    Text("$${String.format("%.2f", if (tradeCredits.isNotEmpty()) productsSubtotal else state.subtotal)}", color = Color(0xFF888888), fontSize = 12.sp)
                                }
                                if (tradeCredits.isNotEmpty()) {
                                    tradeCredits.forEach { credit ->
                                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                            Row(verticalAlignment = Alignment.CenterVertically) {
                                                Surface(color = Color(0xFF60A5FA).copy(alpha = 0.15f), shape = RoundedCornerShape(4.dp)) {
                                                    Text("CREDIT", modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp), color = Color(0xFF60A5FA), fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                                }
                                                Spacer(Modifier.width(6.dp))
                                                Text(credit.name.ifBlank { "Trade Credit" }, color = Color(0xFF60A5FA), fontSize = 11.sp)
                                            }
                                            Text("-$${String.format("%.2f", -credit.price * credit.quantity)}", color = Color(0xFF60A5FA), fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                }
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                    Text("Tax", color = Color(0xFF888888), fontSize = 12.sp)
                                    Text("$${String.format("%.2f", state.taxAmount)}", color = Color(0xFF888888), fontSize = 12.sp)
                                }
                                if (selectedTip > 0) {
                                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                        Text("Tip", color = Color(0xFF888888), fontSize = 12.sp)
                                        Text("$${String.format("%.2f", selectedTip)}", color = Color(0xFF888888), fontSize = 12.sp)
                                    }
                                }
                            }
                            Spacer(Modifier.width(20.dp))
                            val totalPulse = rememberInfiniteTransition(label = "totalPulse")
                            val totalGlow by totalPulse.animateFloat(
                                initialValue = 0.7f, targetValue = 1f,
                                animationSpec = infiniteRepeatable(tween(1200, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "tg"
                            )
                            val totalScale by totalPulse.animateFloat(
                                initialValue = 1f, targetValue = 1.03f,
                                animationSpec = infiniteRepeatable(tween(1200, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ts"
                            )
                            Column(horizontalAlignment = Alignment.End, modifier = Modifier.graphicsLayer(scaleX = totalScale, scaleY = totalScale)) {
                                Text("TOTAL", color = Color(0xFF666666), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                Text("$${String.format("%.2f", total.coerceAtLeast(0.0))}", color = Gold.copy(alpha = totalGlow), fontWeight = FontWeight.Black, fontSize = 28.sp)
                            }
                        }
                    }
                }

                Column(Modifier.padding(horizontal = 24.dp, vertical = 16.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            Icon(
                                if (printReceipt) Icons.Default.Print else Icons.Default.PrintDisabled,
                                contentDescription = null, tint = if (printReceipt) Gold else Color.Gray, modifier = Modifier.size(18.dp)
                            )
                            Text(if (printReceipt) "Print receipt" else "No receipt", color = if (printReceipt) Color.White else Color(0xFF666666), fontSize = 12.sp)
                        }
                        Switch(
                            checked = printReceipt, onCheckedChange = { printReceipt = it },
                            colors = SwitchDefaults.colors(checkedThumbColor = Color.Black, checkedTrackColor = Gold, uncheckedThumbColor = Color.Gray, uncheckedTrackColor = Color(0xFF2A2A2A))
                        )
                    }

                    Spacer(Modifier.height(8.dp))

                    // Text receipt toggle
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            Icon(
                                Icons.Default.Sms,
                                contentDescription = null,
                                tint = if (textReceipt) Gold else Color.Gray,
                                modifier = Modifier.size(18.dp)
                            )
                            Text(
                                if (textReceipt) "Text receipt" else "No text receipt",
                                color = if (textReceipt) Color.White else Color(0xFF666666),
                                fontSize = 12.sp
                            )
                        }
                        Switch(
                            checked = textReceipt,
                            onCheckedChange = {
                                textReceipt = it
                                if (!it) customerPhone = ""
                            },
                            colors = SwitchDefaults.colors(checkedThumbColor = Color.Black, checkedTrackColor = Gold, uncheckedThumbColor = Color.Gray, uncheckedTrackColor = Color(0xFF2A2A2A))
                        )
                    }

                    // Animated phone number input
                    AnimatedVisibility(
                        visible = textReceipt,
                        enter = expandVertically(tween(220)) + fadeIn(tween(220)),
                        exit = shrinkVertically(tween(180)) + fadeOut(tween(180))
                    ) {
                        Column {
                            Spacer(Modifier.height(8.dp))
                            OutlinedTextField(
                                value = customerPhone,
                                onValueChange = { v ->
                                    val digits = v.filter { it.isDigit() || it == '+' || it == '-' || it == ' ' || it == '(' || it == ')' }
                                    if (digits.length <= 15) customerPhone = digits
                                },
                                modifier = Modifier.fillMaxWidth(),
                                label = { Text("Customer phone number", fontSize = 12.sp) },
                                placeholder = { Text("e.g. 555-867-5309", fontSize = 12.sp, color = Color(0xFF555555)) },
                                singleLine = true,
                                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone, imeAction = ImeAction.Done),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Gold,
                                    unfocusedBorderColor = Color(0xFF333333),
                                    focusedLabelColor = Gold,
                                    unfocusedLabelColor = Color(0xFF555555),
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    cursorColor = Gold
                                ),
                                shape = RoundedCornerShape(10.dp),
                                leadingIcon = {
                                    Icon(Icons.Default.Phone, contentDescription = null, tint = Gold, modifier = Modifier.size(18.dp))
                                },
                                trailingIcon = if (customerPhone.isNotEmpty()) {
                                    { IconButton(onClick = { customerPhone = "" }) { Icon(Icons.Default.Clear, contentDescription = null, tint = Color.Gray, modifier = Modifier.size(16.dp)) } }
                                } else null
                            )
                        }
                    }

                    Spacer(Modifier.height(14.dp))
                    Text("TIP", color = Color(0xFF666666), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp)
                    Spacer(Modifier.height(6.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        tipPercents.forEach { pct ->
                            val tipAmt = state.subtotal * pct / 100.0
                            val isSelected = selectedTip == tipAmt
                            OutlinedButton(
                                onClick = { selectedTip = tipAmt },
                                modifier = Modifier.weight(1f).height(38.dp),
                                shape = RoundedCornerShape(8.dp),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    containerColor = if (isSelected) Gold.copy(alpha = 0.15f) else Color.Transparent,
                                    contentColor = if (isSelected) Gold else Color.Gray
                                ),
                                border = BorderStroke(1.dp, if (isSelected) Gold else Color(0xFF333333)),
                                contentPadding = PaddingValues(0.dp)
                            ) {
                                Text(if (pct == 0) "None" else "$pct%", fontSize = 11.sp, fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal)
                            }
                        }
                    }

                    Spacer(Modifier.height(18.dp))

                    if (total <= 0.0) {
                        Button(
                            onClick = { onComplete(PaymentMethod.STORE_CREDIT, 0.0, 0.0, printReceipt); onDismiss() },
                            modifier = Modifier.fillMaxWidth().height(56.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1A4D2E)),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Icon(Icons.Default.CheckCircle, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(20.dp))
                            Spacer(Modifier.width(10.dp))
                            Text("COMPLETE — Fully Covered by Credit", color = Color(0xFF4ADE80), fontWeight = FontWeight.Black, fontSize = 13.sp)
                        }
                        Spacer(Modifier.height(12.dp))
                        HorizontalDivider(color = Color(0xFF2A2A2A))
                        Spacer(Modifier.height(12.dp))
                    }

                    Text("PAYMENT METHOD", color = Color(0xFF666666), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp)
                    Spacer(Modifier.height(10.dp))

                    var showStoreCreditPanel by remember { mutableStateOf(false) }
                    var creditSearchQuery by remember { mutableStateOf("") }
                    var selectedCreditCustomer by remember { mutableStateOf<CustomerEntity?>(null) }
                    val creditSearchResults = remember(creditSearchQuery, state.customers) {
                        if (creditSearchQuery.isEmpty()) state.customers.take(6)
                        else state.customers.filter { it.name.contains(creditSearchQuery, ignoreCase = true) }.take(6)
                    }

                    @Composable fun PayTile(icon: @Composable () -> Unit, label: String, sublabel: String, bgColor: Color, onClick: () -> Unit, modifier: Modifier = Modifier, selected: Boolean = false) {
                        val pulseAnim = rememberInfiniteTransition(label = "payPulse")
                        val pulseScale by pulseAnim.animateFloat(
                            initialValue = 1f, targetValue = 1.06f,
                            animationSpec = infiniteRepeatable(tween(600, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ps"
                        )
                        val glowAlpha by pulseAnim.animateFloat(
                            initialValue = 0.4f, targetValue = 1f,
                            animationSpec = infiniteRepeatable(tween(600, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ga"
                        )
                        Surface(
                            onClick = onClick,
                            color = if (selected) bgColor else bgColor.copy(alpha = 0.7f),
                            shape = RoundedCornerShape(12.dp),
                            border = if (selected) BorderStroke(2.dp, Gold.copy(alpha = glowAlpha)) else null,
                            modifier = modifier.height(82.dp)
                                .graphicsLayer(scaleX = if (selected) pulseScale else 1f, scaleY = if (selected) pulseScale else 1f)
                        ) {
                            Column(Modifier.fillMaxSize().padding(10.dp), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
                                icon()
                                Spacer(Modifier.height(5.dp))
                                Text(label, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                                Text(sublabel, color = Color.White.copy(alpha = 0.5f), fontSize = 9.sp)
                            }
                        }
                    }

                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        PayTile(
                            icon = { Icon(Icons.Default.CreditCard, null, tint = Color.White, modifier = Modifier.size(20.dp)) },
                            label = "CARD", sublabel = "Zettle reader",
                            bgColor = Color(0xFF0A4D82),
                            onClick = {
                                showCashPad = false; showStoreCreditPanel = false
                                if (!ZettleSDK.isInitialized) { Toast.makeText(context, "Zettle SDK not ready", Toast.LENGTH_SHORT).show(); return@PayTile }
                                if (ZettleSDK.instance?.isLoggedIn != true) { Toast.makeText(context, "Not logged in to Zettle. Go to Settings → ZETTLE ACCOUNT → Log In first.", Toast.LENGTH_LONG).show(); return@PayTile }
                                val btAdapter = (context.getSystemService(android.content.Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
                                if (btAdapter == null || !btAdapter.isEnabled) { Toast.makeText(context, "Bluetooth is off. Enable Bluetooth then tap CARD again.", Toast.LENGTH_LONG).show(); return@PayTile }
                                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && (ContextCompat.checkSelfPermission(context, android.Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED || ContextCompat.checkSelfPermission(context, android.Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED)) { Toast.makeText(context, "Bluetooth permission denied. Go to Settings → Apps → HanryxVault → Permissions.", Toast.LENGTH_LONG).show(); return@PayTile }
                                try {
                                    val amountCents = (total * 100).toLong()
                                    val reference = UUID.randomUUID().toString().take(16)
                                    val action = CardReaderAction.Payment(reference = TransactionReference.Builder(reference).build(), amount = amountCents, tippingConfiguration = TippingConfiguration())
                                    val intent = ZettleIntentHelper.charge(action, context)
                                    onZettleLaunched(printReceipt); paymentLauncher.launch(intent); onDismiss()
                                } catch (e: Exception) { Toast.makeText(context, "Card reader error: ${e.message}", Toast.LENGTH_LONG).show() }
                            },
                            modifier = Modifier.weight(1f)
                        )
                        PayTile(
                            icon = { Icon(Icons.Default.Payments, null, tint = Color(0xFF4CAF50), modifier = Modifier.size(20.dp)) },
                            label = "CASH", sublabel = "Cash tender",
                            bgColor = Color(0xFF1A2A1A),
                            onClick = { showCashPad = !showCashPad; showStoreCreditPanel = false },
                            modifier = Modifier.weight(1f),
                            selected = showCashPad
                        )
                        PayTile(
                            icon = { Icon(Icons.Default.AccountBalanceWallet, null, tint = Color(0xFF009CDE), modifier = Modifier.size(20.dp)) },
                            label = "PAYPAL", sublabel = "QR scan",
                            bgColor = Color(0xFF003087),
                            onClick = {
                                showCashPad = false; showStoreCreditPanel = false; manualCardMode = false
                                payPalError = ""; payPalPaid = false; payPalLoading = true; showPayPalDialog = true
                                payPalScope.launch {
                                    try {
                                        val order = PayPalClient.createOrder(clientId = BuildConfig.PAYPAL_CLIENT_ID, clientSecret = BuildConfig.PAYPAL_CLIENT_SECRET, subtotal = state.subtotal, taxAmount = state.taxAmount, tipAmount = selectedTip)
                                        payPalOrderId = order.id; payPalApproveUrl = order.approveUrl; payPalLoading = false
                                        while (showPayPalDialog && !payPalPaid) {
                                            kotlinx.coroutines.delay(3000)
                                            val status = try { PayPalClient.checkOrderStatus(BuildConfig.PAYPAL_CLIENT_ID, BuildConfig.PAYPAL_CLIENT_SECRET, payPalOrderId) } catch (_: Exception) { "" }
                                            if (status == "APPROVED" || status == "COMPLETED") {
                                                if (status == "APPROVED") { PayPalClient.captureOrder(BuildConfig.PAYPAL_CLIENT_ID, BuildConfig.PAYPAL_CLIENT_SECRET, payPalOrderId) }
                                                payPalPaid = true; showPayPalDialog = false; onComplete(PaymentMethod.PAYPAL, total, 0.0, printReceipt); onDismiss()
                                            }
                                        }
                                    } catch (e: Exception) { payPalLoading = false; payPalError = e.message ?: "PayPal error" }
                                }
                            },
                            modifier = Modifier.weight(1f)
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        PayTile(
                            icon = { Icon(Icons.Default.AccountBalanceWallet, null, tint = Color.White, modifier = Modifier.size(20.dp)) },
                            label = "VENMO", sublabel = "QR auto-detect",
                            bgColor = Color(0xFF008CFF),
                            onClick = {
                                showCashPad = false; showStoreCreditPanel = false; manualCardMode = false
                                if (ZettleSDK.isInitialized) {
                                    try {
                                        val amountCents = (total * 100).toLong()
                                        val reference = UUID.randomUUID().toString()
                                        val action = VenmoQrcAction.Payment(amountCents, reference)
                                        val intent = ZettleIntentHelper.charge(action, context)
                                        onZettleLaunched(printReceipt); venmoPaymentLauncher.launch(intent); onDismiss()
                                    } catch (e: Exception) { Toast.makeText(context, "Venmo QRC error: ${e.message}", Toast.LENGTH_LONG).show() }
                                } else { Toast.makeText(context, "Zettle not initialized", Toast.LENGTH_SHORT).show() }
                            },
                            modifier = Modifier.weight(1f)
                        )
                        PayTile(
                            icon = { Icon(Icons.Default.AttachMoney, null, tint = Color.Black, modifier = Modifier.size(20.dp)) },
                            label = "CASH APP", sublabel = "QR scan",
                            bgColor = Color(0xFF00D632),
                            onClick = { showCashPad = false; showStoreCreditPanel = false; manualCardMode = false; showCashAppDialog = true },
                            modifier = Modifier.weight(1f)
                        )
                        PayTile(
                            icon = { Icon(Icons.Default.CardGiftcard, null, tint = Color(0xFFCE93D8), modifier = Modifier.size(20.dp)) },
                            label = "CREDIT", sublabel = "Store credit",
                            bgColor = Color(0xFF1E1230),
                            onClick = { showStoreCreditPanel = !showStoreCreditPanel; showCashPad = false },
                            modifier = Modifier.weight(1f),
                            selected = showStoreCreditPanel
                        )
                    }

                // PayPal QR Dialog
                if (showPayPalDialog) {
                    Dialog(onDismissRequest = { showPayPalDialog = false }) {
                        Surface(
                            shape = RoundedCornerShape(16.dp),
                            color = Color(0xFF0D0D1A),
                            modifier = Modifier.padding(8.dp)
                        ) {
                            Column(
                                Modifier.padding(20.dp),
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                Text("PayPal Payment", color = Color(0xFF009CDE), fontWeight = FontWeight.Bold, fontSize = 16.sp, letterSpacing = 1.sp)
                                Text("$${String.format("%.2f", total)}", color = Color.White, fontWeight = FontWeight.Black, fontSize = 28.sp)
                                HorizontalDivider(color = Color(0xFF1A1A3A))

                                when {
                                    payPalLoading -> {
                                        CircularProgressIndicator(color = Color(0xFF009CDE), modifier = Modifier.padding(24.dp))
                                        Text("Creating PayPal order...", color = Color(0xFF8888AA), fontSize = 12.sp)
                                    }
                                    payPalError.isNotEmpty() -> {
                                        Icon(Icons.Default.ErrorOutline, null, tint = Color(0xFFFF5555), modifier = Modifier.size(48.dp))
                                        Text(payPalError, color = Color(0xFFFF5555), fontSize = 12.sp, textAlign = TextAlign.Center)
                                        Button(onClick = { showPayPalDialog = false }, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF333355))) {
                                            Text("Close")
                                        }
                                    }
                                    payPalApproveUrl.isNotEmpty() -> {
                                        val qrBitmap = remember(payPalApproveUrl) {
                                            try {
                                                val writer = QRCodeWriter()
                                                val matrix = writer.encode(payPalApproveUrl, BarcodeFormat.QR_CODE, 512, 512)
                                                val bmp = Bitmap.createBitmap(512, 512, Bitmap.Config.RGB_565)
                                                for (x in 0 until 512) for (y in 0 until 512)
                                                    bmp.setPixel(x, y, if (matrix[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
                                                bmp
                                            } catch (_: Exception) { null }
                                        }
                                        if (qrBitmap != null) {
                                            androidx.compose.foundation.Image(
                                                bitmap = qrBitmap.asImageBitmap(),
                                                contentDescription = "PayPal QR Code",
                                                modifier = Modifier.size(220.dp).clip(RoundedCornerShape(8.dp))
                                            )
                                        }
                                        Text("Have the customer scan this with their PayPal app", color = Color(0xFF8888AA), fontSize = 11.sp, textAlign = TextAlign.Center)
                                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                            CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp, color = Color(0xFF009CDE))
                                            Text("Waiting for payment — auto-detects when paid", color = Color(0xFF6688AA), fontSize = 11.sp)
                                        }
                                        HorizontalDivider(color = Color(0xFF1A1A3A))
                                        OutlinedButton(
                                            onClick = { showPayPalDialog = false },
                                            modifier = Modifier.fillMaxWidth(),
                                            colors = ButtonColors(containerColor = Color.Transparent, contentColor = Color(0xFF8888AA), disabledContainerColor = Color.Transparent, disabledContentColor = Color.Gray)
                                        ) { Text("Cancel payment") }
                                    }
                                }
                            }
                        }
                    }
                }

                // Cash App QR Dialog
                if (showCashAppDialog) {
                    val cashTag = CashAppHandlePreference.get(context)
                    val amountStr = String.format("%.2f", total)
                    val cashAppUrl = if (cashTag.isNotBlank()) "https://cash.app/\$$cashTag/$amountStr" else ""
                    Dialog(onDismissRequest = { showCashAppDialog = false }) {
                        Surface(
                            shape = RoundedCornerShape(16.dp),
                            color = Color(0xFF0D0D1A),
                            modifier = Modifier.padding(8.dp)
                        ) {
                            Column(
                                Modifier.padding(20.dp),
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                Text("Cash App Payment", color = Color(0xFF00D632), fontWeight = FontWeight.Bold, fontSize = 16.sp, letterSpacing = 1.sp)
                                Text("$$amountStr", color = Color.White, fontWeight = FontWeight.Black, fontSize = 28.sp)
                                HorizontalDivider(color = Color(0xFF1A1A3A))
                                if (cashTag.isBlank()) {
                                    Icon(Icons.Default.ErrorOutline, null, tint = Color(0xFFFF5555), modifier = Modifier.size(48.dp))
                                    Text("No Cash App \$Cashtag set.\nGo to Settings → Cash App Handle.", color = Color(0xFFFF5555), fontSize = 12.sp, textAlign = TextAlign.Center)
                                } else {
                                    val qrBitmap = remember(cashAppUrl) {
                                        try {
                                            val writer = QRCodeWriter()
                                            val matrix = writer.encode(cashAppUrl, BarcodeFormat.QR_CODE, 512, 512)
                                            val bmp = Bitmap.createBitmap(512, 512, Bitmap.Config.RGB_565)
                                            for (x in 0 until 512) for (y in 0 until 512)
                                                bmp.setPixel(x, y, if (matrix[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
                                            bmp
                                        } catch (_: Exception) { null }
                                    }
                                    if (qrBitmap != null) {
                                        androidx.compose.foundation.Image(
                                            bitmap = qrBitmap.asImageBitmap(),
                                            contentDescription = "Cash App QR Code",
                                            modifier = Modifier.size(220.dp).clip(RoundedCornerShape(8.dp))
                                        )
                                    }
                                    Text("Have the customer scan with their camera or Cash App", color = Color(0xFF8888AA), fontSize = 11.sp, textAlign = TextAlign.Center)
                                    Text("\$$cashTag", color = Color(0xFF00D632), fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                }
                                HorizontalDivider(color = Color(0xFF1A1A3A))
                                Button(
                                    onClick = {
                                        showCashAppDialog = false
                                        onComplete(PaymentMethod.CASH_APP, total, 0.0, printReceipt)
                                        onDismiss()
                                    },
                                    modifier = Modifier.fillMaxWidth().height(50.dp),
                                    shape = RoundedCornerShape(10.dp),
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF00D632)),
                                    enabled = cashTag.isNotBlank()
                                ) {
                                    Icon(Icons.Default.CheckCircle, null, tint = Color.Black, modifier = Modifier.size(18.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("CONFIRM RECEIVED", fontWeight = FontWeight.Black, color = Color.Black)
                                }
                                OutlinedButton(
                                    onClick = { showCashAppDialog = false },
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonColors(containerColor = Color.Transparent, contentColor = Color(0xFF8888AA), disabledContainerColor = Color.Transparent, disabledContentColor = Color.Gray)
                                ) { Text("Cancel") }
                            }
                        }
                    }
                }

                // Manual card confirm (shown when Zettle reader is not connected)
                AnimatedVisibility(visible = manualCardMode) {
                    Column(Modifier.padding(top = 10.dp)) {
                        Surface(color = Color(0xFF0A1F35), shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                    Text("MANUAL CARD", color = Color(0xFF64B5F6), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    Text("$${String.format("%.2f", total)}", color = Color(0xFF64B5F6), fontSize = 18.sp, fontWeight = FontWeight.Black)
                                }
                                Text("Zettle reader not connected. Tap Confirm to record this as a card sale.", color = Color(0xFF5A8AAA), fontSize = 12.sp, lineHeight = 16.sp)
                                HorizontalDivider(color = Color(0xFF1A3A55))
                                Button(
                                    onClick = { onComplete(PaymentMethod.CARD, total, 0.0, printReceipt); onDismiss() },
                                    modifier = Modifier.fillMaxWidth().height(50.dp),
                                    shape = RoundedCornerShape(10.dp),
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF0A4D82))
                                ) {
                                    Icon(Icons.Default.CreditCard, null, tint = Color.White, modifier = Modifier.size(18.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("CONFIRM CARD PAYMENT", fontWeight = FontWeight.Black, color = Color.White)
                                }
                            }
                        }
                    }
                }

                // Store Credit — customer search panel
                AnimatedVisibility(visible = showStoreCreditPanel) {
                    Column(Modifier.padding(top = 12.dp)) {
                        Surface(color = Color(0xFF1A0D2A), shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                Text("STORE CREDIT", color = Color(0xFFCE93D8), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                HorizontalDivider(color = Color(0xFF3D1A5A))

                                if (selectedCreditCustomer == null) {
                                    // Search field
                                    OutlinedTextField(
                                        value = creditSearchQuery,
                                        onValueChange = { creditSearchQuery = it },
                                        label = { Text("Search customer by name", fontSize = 12.sp) },
                                        singleLine = true,
                                        modifier = Modifier.fillMaxWidth(),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            focusedBorderColor = Color(0xFFCE93D8),
                                            unfocusedBorderColor = Color(0xFF3D1A5A),
                                            focusedLabelColor = Color(0xFFCE93D8),
                                            unfocusedLabelColor = Color(0xFF666666)
                                        ),
                                        leadingIcon = { Icon(Icons.Default.Search, null, tint = Color(0xFF9C5BB5), modifier = Modifier.size(18.dp)) }
                                    )
                                    if (creditSearchResults.isEmpty()) {
                                        Text("No customers found — add them via the Credits panel", color = Color(0xFF666666), fontSize = 11.sp)
                                    } else {
                                        creditSearchResults.forEach { cust ->
                                            Row(
                                                Modifier
                                                    .fillMaxWidth()
                                                    .clip(RoundedCornerShape(8.dp))
                                                    .background(Color(0xFF2A1040))
                                                    .clickable { selectedCreditCustomer = cust }
                                                    .padding(horizontal = 12.dp, vertical = 10.dp),
                                                horizontalArrangement = Arrangement.SpaceBetween,
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    Icon(Icons.Default.Person, null, tint = Color(0xFF9C5BB5), modifier = Modifier.size(16.dp))
                                                    Spacer(Modifier.width(8.dp))
                                                    Text(cust.name, color = Color.White, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                                                }
                                                Text("$${String.format("%.2f", cust.storeCredit)}", color = Color(0xFFCE93D8), fontWeight = FontWeight.Black, fontSize = 14.sp)
                                            }
                                        }
                                    }
                                } else {
                                    // Selected customer — show balance and confirm
                                    val cust = selectedCreditCustomer!!
                                    val hasEnough = cust.storeCredit >= total
                                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Icon(Icons.Default.Person, null, tint = Color(0xFF9C5BB5), modifier = Modifier.size(18.dp))
                                            Spacer(Modifier.width(8.dp))
                                            Column {
                                                Text(cust.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                                                Text("Balance: $${String.format("%.2f", cust.storeCredit)}", color = if (hasEnough) Color(0xFF4CAF50) else Color(0xFFEF5350), fontSize = 11.sp)
                                            }
                                        }
                                        TextButton(onClick = { selectedCreditCustomer = null; creditSearchQuery = "" }) {
                                            Text("Change", color = Color(0xFF9C5BB5), fontSize = 11.sp)
                                        }
                                    }
                                    if (!hasEnough) {
                                        Surface(color = Color(0xFF2B0D0D), shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
                                            Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                                                Icon(Icons.Default.Warning, null, tint = Color(0xFFEF5350), modifier = Modifier.size(16.dp))
                                                Spacer(Modifier.width(8.dp))
                                                Text("Insufficient credit. Balance: $${String.format("%.2f", cust.storeCredit)} — need $${String.format("%.2f", total)}", color = Color(0xFFEF5350), fontSize = 11.sp)
                                            }
                                        }
                                    }
                                    Button(
                                        onClick = {
                                            if (hasEnough) {
                                                onDeductCustomerCredit(cust.id, total)
                                                onComplete(PaymentMethod.STORE_CREDIT, total, 0.0, printReceipt)
                                                onDismiss()
                                            }
                                        },
                                        enabled = hasEnough,
                                        modifier = Modifier.fillMaxWidth().height(50.dp),
                                        shape = RoundedCornerShape(10.dp),
                                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A1B9A), disabledContainerColor = Color(0xFF2A1040))
                                    ) {
                                        Icon(Icons.Default.CheckCircle, null, tint = Color.White, modifier = Modifier.size(18.dp))
                                        Spacer(Modifier.width(8.dp))
                                        Text(
                                            if (hasEnough) "APPLY $${String.format("%.2f", total)} CREDIT" else "INSUFFICIENT BALANCE",
                                            fontWeight = FontWeight.Black,
                                            color = if (hasEnough) Color.White else Color(0xFF555555)
                                        )
                                    }
                                }
                            }
                        }
                    }
                }

                // Cash numpad (shown when cash is selected)
                AnimatedVisibility(visible = showCashPad) {
                    Column(Modifier.padding(top = 16.dp)) {
                        // Amount display
                        val shortfall = (total - cashReceived).coerceAtLeast(0.0)
                        val isEnough = cashReceived >= total
                        Surface(
                            color = when {
                                isEnough -> Color(0xFF0D2B0D)
                                cashEntry.isNotEmpty() -> Color(0xFF2B0D0D)
                                else -> Color(0xFF1A1A1A)
                            },
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Column(Modifier.padding(12.dp)) {
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                    Text("CASH RECEIVED", color = Color.Gray, fontSize = 10.sp, letterSpacing = 1.sp)
                                    Text("TOTAL DUE  $${String.format("%.2f", total)}", color = Color.Gray, fontSize = 10.sp)
                                }
                                Spacer(Modifier.height(4.dp))
                                Text(
                                    if (cashEntry.isEmpty()) "$0.00" else "$$cashEntry",
                                    color = if (isEnough) Color(0xFF4CAF50) else Color.White,
                                    fontSize = 28.sp,
                                    fontWeight = FontWeight.Black
                                )
                                Spacer(Modifier.height(6.dp))
                                HorizontalDivider(color = Color(0xFF2A2A2A))
                                Spacer(Modifier.height(6.dp))
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                    Text(
                                        if (isEnough) "CHANGE DUE" else if (cashEntry.isEmpty()) "ENTER AMOUNT" else "STILL NEED",
                                        color = if (isEnough) Color(0xFF4CAF50) else if (cashEntry.isEmpty()) Color(0xFF555555) else Color(0xFFEF5350),
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Bold,
                                        letterSpacing = 1.sp
                                    )
                                    Text(
                                        if (isEnough) "$${String.format("%.2f", changeDue)}"
                                        else if (cashEntry.isEmpty()) "—"
                                        else "-$${String.format("%.2f", shortfall)}",
                                        color = if (isEnough) Color(0xFF4CAF50) else if (cashEntry.isEmpty()) Color(0xFF555555) else Color(0xFFEF5350),
                                        fontSize = 20.sp,
                                        fontWeight = FontWeight.Black
                                    )
                                }
                            }
                        }
                        Spacer(Modifier.height(12.dp))
                        // Quick amounts
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf(total, 20.0, 50.0, 100.0).forEach { amt ->
                                val label = if (amt == total) "Exact" else "$${"%.0f".format(amt)}"
                                OutlinedButton(
                                    onClick = { cashEntry = String.format("%.2f", amt) },
                                    modifier = Modifier.weight(1f).height(36.dp),
                                    shape = RoundedCornerShape(8.dp),
                                    contentPadding = PaddingValues(0.dp),
                                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.White),
                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF333333))
                                ) { Text(label, fontSize = 11.sp) }
                            }
                        }
                        Spacer(Modifier.height(8.dp))
                        // Numpad grid (plain rows — avoids nested scroll conflict)
                        val keyRows = listOf(
                            listOf("1","2","3"),
                            listOf("4","5","6"),
                            listOf("7","8","9"),
                            listOf(".","0","⌫")
                        )
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            keyRows.forEach { row ->
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    row.forEach { key ->
                                        Box(
                                            Modifier
                                                .weight(1f)
                                                .height(40.dp)
                                                .clip(RoundedCornerShape(8.dp))
                                                .background(if (key == "⌫") Color(0xFF2A1A1A) else Color(0xFF2A2A2A))
                                                .clickable { if (key == "⌫") backspace() else appendDigit(key) },
                                            contentAlignment = Alignment.Center
                                        ) {
                                            Text(key, color = if (key == "⌫") Color(0xFFE57373) else Color.White, fontSize = 16.sp, fontWeight = FontWeight.Medium)
                                        }
                                    }
                                }
                            }
                        }
                        Spacer(Modifier.height(12.dp))

                        // ── RECEIPT OPTIONS — inline within the cash pad so
                        // they're impossible to miss right before confirming.
                        // These mirror the toggles at the top of the modal
                        // (shared state), giving the operator one last chance
                        // to enable/disable a printed or texted receipt.
                        Surface(
                            color = Color(0xFF1A1A1A),
                            shape = RoundedCornerShape(10.dp),
                            border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF2A2A2A)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                    Text("RECEIPT", color = Color(0xFF888888), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        // Print toggle chip
                                        FilterChip(
                                            selected = printReceipt,
                                            onClick = { printReceipt = !printReceipt },
                                            label = {
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    Icon(
                                                        if (printReceipt) Icons.Default.Print else Icons.Default.PrintDisabled,
                                                        contentDescription = null,
                                                        tint = if (printReceipt) Color.Black else Color.Gray,
                                                        modifier = Modifier.size(14.dp)
                                                    )
                                                    Spacer(Modifier.width(4.dp))
                                                    Text("Print", fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                                }
                                            },
                                            colors = FilterChipDefaults.filterChipColors(
                                                selectedContainerColor = Gold,
                                                selectedLabelColor = Color.Black,
                                                containerColor = Color(0xFF222222),
                                                labelColor = Color.Gray
                                            )
                                        )
                                        // Text toggle chip
                                        FilterChip(
                                            selected = textReceipt,
                                            onClick = {
                                                textReceipt = !textReceipt
                                                if (!textReceipt) customerPhone = ""
                                            },
                                            label = {
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    Icon(
                                                        Icons.Default.Sms,
                                                        contentDescription = null,
                                                        tint = if (textReceipt) Color.Black else Color.Gray,
                                                        modifier = Modifier.size(14.dp)
                                                    )
                                                    Spacer(Modifier.width(4.dp))
                                                    Text("Text", fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                                }
                                            },
                                            colors = FilterChipDefaults.filterChipColors(
                                                selectedContainerColor = Gold,
                                                selectedLabelColor = Color.Black,
                                                containerColor = Color(0xFF222222),
                                                labelColor = Color.Gray
                                            )
                                        )
                                    }
                                }
                                AnimatedVisibility(
                                    visible = textReceipt,
                                    enter = expandVertically(tween(180)) + fadeIn(tween(180)),
                                    exit = shrinkVertically(tween(140)) + fadeOut(tween(140))
                                ) {
                                    Column {
                                        Spacer(Modifier.height(8.dp))
                                        OutlinedTextField(
                                            value = customerPhone,
                                            onValueChange = { v ->
                                                val digits = v.filter { it.isDigit() || it == '+' || it == '-' || it == ' ' || it == '(' || it == ')' }
                                                if (digits.length <= 15) customerPhone = digits
                                            },
                                            modifier = Modifier.fillMaxWidth(),
                                            placeholder = { Text("Customer phone (e.g. 555-867-5309)", fontSize = 11.sp, color = Color(0xFF555555)) },
                                            singleLine = true,
                                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone, imeAction = ImeAction.Done),
                                            colors = OutlinedTextFieldDefaults.colors(
                                                focusedBorderColor = Gold,
                                                unfocusedBorderColor = Color(0xFF333333),
                                                focusedTextColor = Color.White,
                                                unfocusedTextColor = Color.White,
                                                cursorColor = Gold
                                            ),
                                            shape = RoundedCornerShape(8.dp),
                                            textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp),
                                            leadingIcon = {
                                                Icon(Icons.Default.Phone, contentDescription = null, tint = Gold, modifier = Modifier.size(16.dp))
                                            }
                                        )
                                    }
                                }
                            }
                        }
                        Spacer(Modifier.height(10.dp))

                        Button(
                            onClick = { onComplete(PaymentMethod.CASH, cashReceived, changeDue, printReceipt); onDismiss() },
                            enabled = cashReceived >= total,
                            modifier = Modifier.fillMaxWidth().height(52.dp),
                            shape = RoundedCornerShape(12.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32), disabledContainerColor = Color(0xFF1A2A1A))
                        ) {
                            Icon(Icons.Default.Check, null, tint = Color.White, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("CONFIRM CASH PAYMENT", fontWeight = FontWeight.Bold)
                        }
                    }
                }
                }
            }
        }
    }
}

// ── TOP-LEVEL COMPOSABLES (stable — no Activity receiver, Compose can skip freely) ──────────

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun ProductCard(product: ProductEntity, onClick: () -> Unit, onLongClick: () -> Unit = {}) {
    val categoryColor = remember(product.category) {
        val colors = listOf(
            Color(0xFF1A3A2A), Color(0xFF1A2A3A), Color(0xFF3A1A2A),
            Color(0xFF3A2A1A), Color(0xFF2A1A3A), Color(0xFF2A3A1A)
        )
        colors[Math.abs(product.category.hashCode()) % colors.size]
    }
    val formattedPrice = remember(product.price) { "$${"%.2f".format(product.price)}" }

    var pressed by remember { mutableStateOf(false) }
    val tapScale by animateFloatAsState(if (pressed) 0.95f else 1f, spring(dampingRatio = 0.5f, stiffness = 800f), label = "cardTap")
    val tapElevation by animateDpAsState(if (pressed) 0.dp else 2.dp, tween(100), label = "cardElev")

    val enterAnim = remember { Animatable(0f) }
    LaunchedEffect(Unit) { enterAnim.animateTo(1f, tween(350, easing = FastOutSlowInEasing)) }

    Card(
        colors = CardDefaults.cardColors(containerColor = VaultSurface),
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth().height(120.dp)
            .graphicsLayer(
                scaleX = tapScale * enterAnim.value.coerceIn(0.8f, 1f),
                scaleY = tapScale * enterAnim.value.coerceIn(0.8f, 1f),
                alpha = enterAnim.value,
                translationY = (1f - enterAnim.value) * 20f
            )
            .combinedClickable(
                onClick = {
                    pressed = true
                    onClick()
                },
                onLongClick = onLongClick
            ),
        elevation = CardDefaults.cardElevation(defaultElevation = tapElevation)
    ) {
        LaunchedEffect(pressed) { if (pressed) { delay(120); pressed = false } }
        Row(Modifier.fillMaxSize()) {
            Box(Modifier.width(4.dp).fillMaxHeight().background(categoryColor))
            Column(
                Modifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.SpaceBetween
            ) {
                Column {
                    Text(
                        product.name,
                        color = Color.White,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        lineHeight = 18.sp
                    )
                    if (product.category.isNotEmpty()) {
                        Spacer(Modifier.height(2.dp))
                        Text(product.category, color = Color(0xFF555555), fontSize = 10.sp, maxLines = 1)
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text(formattedPrice, color = Gold, fontSize = 17.sp, fontWeight = FontWeight.Black)
                    if (product.stockQuantity > 0) {
                        Text("${product.stockQuantity} left", color = Color(0xFF444444), fontSize = 10.sp)
                    }
                }
            }
        }
    }
}

/** Cycles through trading-card conditions in the order most staff use at checkout. */
fun cycleCondition(current: String): String = when (current) {
    "NM" -> "LP"
    "LP" -> "MP"
    "MP" -> "HP"
    "HP" -> "DMG"
    else -> "NM"
}

@Composable
fun CartItemRow(
    item: CartItemEntity,
    onIncrement: () -> Unit,
    onDecrement: () -> Unit,
    onPriceEdit: () -> Unit = {},
    condition: String = "NM",
    onConditionCycle: () -> Unit = {},
    discountPct: Double = 0.0,
    onDiscount: () -> Unit = {},
    marketPrice: MarketPriceResult? = null,
    isFetchingMarket: Boolean = false,
    onMarketPriceTap: () -> Unit = {},
    onHaggle: () -> Unit = {},
    onEstimate: () -> Unit = {}
) {
    val formattedEach  = remember(item.price, discountPct) {
        if (discountPct > 0.0) "$${"%.2f".format(item.price)} ea  •  ${discountPct.toInt()}% off"
        else "$${"%.2f".format(item.price)} each"
    }
    val formattedTotal = remember(item.price, item.quantity) { "$${"%.2f".format(item.price * item.quantity)}" }
    val conditionColor = when (condition) {
        "NM"  -> Color(0xFF4CAF50)
        "LP"  -> Color(0xFFCDDC39)
        "MP"  -> Color(0xFFFF9800)
        "HP"  -> Color(0xFFF44336)
        "DMG" -> Color(0xFF8D2B1A)
        else  -> Color(0xFF4CAF50)
    }
    val discountActive = discountPct > 0.0

    val mktBadgeColor = when (marketPrice?.price_badge) {
        "HIGH" -> Color(0xFFEF5350)
        "LOW"  -> Color(0xFF66BB6A)
        else   -> Color(0xFF26C6DA)
    }

    val slideIn = remember { Animatable(0f) }
    LaunchedEffect(Unit) { slideIn.animateTo(1f, tween(300, easing = FastOutSlowInEasing)) }

    Column(Modifier.fillMaxWidth().background(VaultGrey)
        .graphicsLayer(alpha = slideIn.value, translationX = (1f - slideIn.value) * 60f)
        .padding(horizontal = 16.dp, vertical = 8.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(item.name, color = Color.White, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
            Spacer(Modifier.width(8.dp))
            Text(formattedTotal, color = Gold, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(4.dp))
        Box(
            Modifier
                .clip(RoundedCornerShape(6.dp))
                .background(if (discountActive) Color(0xFFFF9800).copy(alpha = 0.18f) else Color(0xFF2A2A2A))
                .clickable(onClick = onPriceEdit)
                .padding(horizontal = 8.dp, vertical = 5.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Filled.Edit,
                    contentDescription = "Edit price",
                    tint = if (discountActive) Color(0xFFFF9800) else Gold.copy(alpha = 0.85f),
                    modifier = Modifier.size(12.dp)
                )
                Spacer(Modifier.width(4.dp))
                Text(
                    formattedEach,
                    color = if (discountActive) Color(0xFFFF9800) else Gold.copy(alpha = 0.85f),
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium
                )
            }
        }
        Spacer(Modifier.height(6.dp))
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Box(
                Modifier.clip(RoundedCornerShape(6.dp)).background(conditionColor.copy(alpha = 0.18f)).clickable(onClick = onConditionCycle).padding(horizontal = 7.dp, vertical = 4.dp),
                contentAlignment = Alignment.Center
            ) { Text(condition, color = conditionColor, fontSize = 10.sp, fontWeight = FontWeight.Bold) }
            Box(
                Modifier.clip(RoundedCornerShape(6.dp)).background(if (discountActive) Color(0xFFFF9800).copy(alpha = 0.18f) else Color(0xFF2A2A2A)).clickable(onClick = onDiscount).padding(horizontal = 7.dp, vertical = 4.dp),
                contentAlignment = Alignment.Center
            ) { Text(if (discountActive) "${discountPct.toInt()}%" else "%", color = if (discountActive) Color(0xFFFF9800) else Color(0xFF555555), fontSize = 10.sp, fontWeight = FontWeight.Bold) }
            when {
                isFetchingMarket -> {
                    Box(Modifier.clip(RoundedCornerShape(4.dp)).background(Color(0xFF1A1A1A)).padding(horizontal = 5.dp, vertical = 3.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(modifier = Modifier.size(10.dp), color = Color(0xFF444444), strokeWidth = 1.5.dp)
                    }
                }
                marketPrice != null && marketPrice.weighted_avg > 0 -> {
                    Box(Modifier.clip(RoundedCornerShape(6.dp)).background(mktBadgeColor.copy(alpha = 0.15f)).clickable(onClick = onMarketPriceTap).padding(horizontal = 7.dp, vertical = 4.dp), contentAlignment = Alignment.Center) {
                        Text("Mkt ${"%.2f".format(marketPrice.weighted_avg)}", color = mktBadgeColor, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                }
            }
            Box(
                Modifier.clip(RoundedCornerShape(6.dp)).background(Color(0xFF7C4DFF).copy(alpha = 0.15f)).clickable(onClick = onHaggle).padding(horizontal = 6.dp, vertical = 4.dp),
                contentAlignment = Alignment.Center
            ) { Text("Haggle", color = Color(0xFF7C4DFF), fontSize = 9.sp, fontWeight = FontWeight.Bold) }
            if (marketPrice != null && marketPrice.weighted_avg > 0) {
                Box(
                    Modifier.clip(RoundedCornerShape(6.dp)).background(Color(0xFF00E676).copy(alpha = 0.12f)).clickable(onClick = onEstimate).padding(horizontal = 6.dp, vertical = 4.dp),
                    contentAlignment = Alignment.Center
                ) { Text("Estimate", color = Color(0xFF00E676), fontSize = 9.sp, fontWeight = FontWeight.Bold) }
            }
            Spacer(Modifier.weight(1f))
            Row(verticalAlignment = Alignment.CenterVertically) {
                var decPressed by remember { mutableStateOf(false) }
                val decScale by animateFloatAsState(if (decPressed) 0.8f else 1f, spring(dampingRatio = 0.4f, stiffness = 1000f), label = "dec")
                Box(Modifier.size(30.dp).graphicsLayer(scaleX = decScale, scaleY = decScale).clip(CircleShape).background(Color(0xFF2A2A2A)).clickable { decPressed = true; onDecrement() }, contentAlignment = Alignment.Center) {
                    Text("−", color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.Light)
                }
                LaunchedEffect(decPressed) { if (decPressed) { delay(100); decPressed = false } }
                val qtyAnim by animateIntAsState(item.quantity, tween(200), label = "qty")
                Text("$qtyAnim", color = Color.White, fontSize = 14.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 6.dp), textAlign = TextAlign.Center)
                var incPressed by remember { mutableStateOf(false) }
                val incScale by animateFloatAsState(if (incPressed) 0.8f else 1f, spring(dampingRatio = 0.4f, stiffness = 1000f), label = "inc")
                Box(Modifier.size(30.dp).graphicsLayer(scaleX = incScale, scaleY = incScale).clip(CircleShape).background(Gold.copy(alpha = 0.15f)).clickable { incPressed = true; onIncrement() }, contentAlignment = Alignment.Center) {
                    Text("+", color = Gold, fontSize = 16.sp)
                }
                LaunchedEffect(incPressed) { if (incPressed) { delay(100); incPressed = false } }
            }
        }
    }
    HorizontalDivider(color = Color(0xFF1E1E1E))
}

@Composable
fun SuccessScreen(onDismiss: () -> Unit) {
    val context = LocalContext.current

    // Fire haptic + tone once on appear
    LaunchedEffect(Unit) {
        // Double haptic pulse (ka-thunk ka-thunk)
        val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val vm = context.getSystemService(android.content.Context.VIBRATOR_MANAGER_SERVICE) as? android.os.VibratorManager
            vm?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(android.content.Context.VIBRATOR_SERVICE) as? Vibrator
        }
        vibrator?.let {
            val pattern = VibrationEffect.createWaveform(
                longArrayOf(0L, 90L, 70L, 130L),  // off, buzz, pause, buzz
                intArrayOf(0, 220, 0, 255),         // amplitudes
                -1
            )
            it.vibrate(pattern)
        }

        // Pleasant success tone — two quick ascending beeps, off-main-thread
        withContext(Dispatchers.IO) {
            try {
                val tg = ToneGenerator(AudioManager.STREAM_MUSIC, 85)
                tg.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
                Thread.sleep(200)
                tg.startTone(ToneGenerator.TONE_PROP_BEEP2, 200)
                Thread.sleep(400)
                tg.release()
            } catch (_: Exception) { /* audio not available */ }
        }

        delay(1600)
        onDismiss()
    }

    // Trigger drives all animations from initial → target on first frame
    var triggered by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { triggered = true }

    val overlayAlpha by animateFloatAsState(
        targetValue = if (triggered) 1f else 0f,
        animationSpec = tween(200),
        label = "overlay"
    )
    val ringScale by animateFloatAsState(
        targetValue = if (triggered) 1.7f else 1f,
        animationSpec = tween(650, easing = FastOutLinearInEasing),
        label = "ring"
    )
    val ringAlpha by animateFloatAsState(
        targetValue = if (triggered) 0f else 0.8f,
        animationSpec = tween(650, easing = FastOutLinearInEasing),
        label = "ringAlpha"
    )
    val checkScale by animateFloatAsState(
        targetValue = if (triggered) 1f else 0f,
        animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy, stiffness = Spring.StiffnessMediumLow),
        label = "checkScale"
    )
    val textOffsetY by animateFloatAsState(
        targetValue = if (triggered) 0f else 40f,
        animationSpec = tween(400, delayMillis = 120, easing = FastOutSlowInEasing),
        label = "textY"
    )
    val textAlpha by animateFloatAsState(
        targetValue = if (triggered) 1f else 0f,
        animationSpec = tween(400, delayMillis = 120),
        label = "textAlpha"
    )

    val sparkleAnim = rememberInfiniteTransition(label = "sparkle")
    val sparkleRotation by sparkleAnim.animateFloat(
        initialValue = 0f, targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(3000, easing = LinearEasing)), label = "sr"
    )
    val shimmerOffset by sparkleAnim.animateFloat(
        initialValue = -1f, targetValue = 2f,
        animationSpec = infiniteRepeatable(tween(1200, easing = FastOutSlowInEasing)), label = "so"
    )
    val sparkleAlpha by sparkleAnim.animateFloat(
        initialValue = 0.3f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(500, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "sa"
    )

    Box(
        Modifier.fillMaxSize().alpha(overlayAlpha).background(Color(0xE6000000)),
        contentAlignment = Alignment.Center
    ) {
        val sparklePositions = remember {
            listOf(
                -100f to -140f, 120f to -100f, -80f to 90f, 110f to 120f,
                -140f to -30f, 150f to 10f, 0f to -160f, -50f to 150f,
                60f to -150f, -120f to 130f, 140f to -60f, -30f to -120f
            )
        }
        sparklePositions.forEachIndexed { idx, (dx, dy) ->
            val delay = idx * 120
            val particleScale by sparkleAnim.animateFloat(
                initialValue = 0f, targetValue = 1f,
                animationSpec = infiniteRepeatable(tween(800, delayMillis = delay, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "p$idx"
            )
            Box(
                Modifier
                    .size(4.dp)
                    .graphicsLayer(translationX = dx, translationY = dy, scaleX = particleScale, scaleY = particleScale, alpha = sparkleAlpha, rotationZ = sparkleRotation + idx * 30f)
                    .clip(CircleShape)
                    .background(Gold)
            )
        }

        Box(
            Modifier
                .size(160.dp)
                .graphicsLayer(scaleX = ringScale, scaleY = ringScale, alpha = ringAlpha)
                .clip(CircleShape)
                .background(Gold.copy(alpha = 0.25f))
        )

        Box(
            Modifier
                .size(180.dp)
                .graphicsLayer(scaleX = ringScale * 1.1f, scaleY = ringScale * 1.1f, alpha = ringAlpha * 0.5f)
                .clip(CircleShape)
                .border(1.dp, Gold.copy(alpha = 0.3f), CircleShape)
        )

        Box(
            Modifier
                .size(148.dp)
                .graphicsLayer(scaleX = ringScale * 0.9f, scaleY = ringScale * 0.9f, alpha = ringAlpha * 1.4f)
                .clip(CircleShape)
                .border(2.dp, Gold.copy(alpha = 0.7f), CircleShape)
        )

        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Box(
                Modifier
                    .size(120.dp)
                    .graphicsLayer(scaleX = checkScale, scaleY = checkScale)
                    .clip(CircleShape)
                    .background(
                        Brush.radialGradient(
                            colors = listOf(Color(0xFF2A1F00), Color(0xFF1A1200), Color(0xFF0D0900)),
                            radius = 200f
                        )
                    ),
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    Icons.Default.CheckCircle,
                    contentDescription = "Sale complete",
                    tint = Gold,
                    modifier = Modifier.size(100.dp)
                )
            }

            Spacer(Modifier.height(20.dp))

            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier
                    .graphicsLayer(translationY = textOffsetY + 30f)
                    .alpha(textAlpha)
            ) {
                Text(
                    "SALE COMPLETE",
                    color = Gold,
                    fontSize = 22.sp,
                    fontWeight = FontWeight.Black,
                    letterSpacing = 4.sp
                )
                Spacer(Modifier.height(2.dp))
                Box(
                    Modifier
                        .width(140.dp)
                        .height(2.dp)
                        .background(
                            Brush.horizontalGradient(
                                colors = listOf(Color.Transparent, Gold.copy(alpha = 0.6f), Color.Transparent),
                                startX = shimmerOffset * 200f,
                                endX = shimmerOffset * 200f + 100f
                            )
                        )
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    "Transaction recorded successfully",
                    color = Color(0xFF888888),
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Normal
                )
            }
        }
    }
}

// ── ADD PRODUCT DIALOG ─────────────────────────────────────────────────────────
@Composable
fun AddProductDialog(
    categories: List<String>,
    qrCode: String = "",
    onScanQrCode: () -> Unit = {},
    onConfirm: (name: String, price: Double, category: String, qrCode: String) -> Unit,
    onDismiss: () -> Unit
) {
    var name     by remember { mutableStateOf("") }
    var priceStr by remember { mutableStateOf("") }
    var category by remember { mutableStateOf(categories.firstOrNull() ?: "") }
    // Syncs with parent when a scan result arrives, but still locally editable
    var qrCodeInput by remember(qrCode) { mutableStateOf(qrCode) }
    var customCategory by remember { mutableStateOf("") }
    val useCustomCategory = categories.isEmpty()

    val price = priceStr.toDoubleOrNull() ?: 0.0
    val canConfirm = name.isNotBlank() && price > 0 && (category.isNotBlank() || customCategory.isNotBlank())

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = VaultSurface,
        shape = RoundedCornerShape(16.dp),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Inventory2, null, tint = Gold, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text("ADD PRODUCT", color = Color.White, fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 2.sp)
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                val fieldColors = OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White, unfocusedTextColor = Color.White,
                    focusedBorderColor = Gold, unfocusedBorderColor = Color(0xFF444444),
                    focusedLabelColor = Gold, unfocusedLabelColor = Color.Gray
                )
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("Product Name *") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors,
                    leadingIcon = { Icon(Icons.Default.Label, null, tint = Color.Gray, modifier = Modifier.size(18.dp)) }
                )
                OutlinedTextField(
                    value = priceStr,
                    onValueChange = { priceStr = it },
                    label = { Text("Price *") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors,
                    leadingIcon = { Text("$", color = Color.Gray, fontWeight = FontWeight.Bold, modifier = Modifier.padding(start = 4.dp)) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal)
                )
                if (!useCustomCategory && categories.isNotEmpty()) {
                    Text("Category", color = Color.Gray, fontSize = 12.sp)
                    androidx.compose.foundation.lazy.LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        items(categories + listOf("+ New"), key = { it }) { cat ->
                            val isSelected = cat == category
                            Surface(
                                color = if (isSelected) Gold else Color(0xFF2A2A2A),
                                shape = RoundedCornerShape(20.dp),
                                modifier = Modifier.clickable {
                                    if (cat == "+ New") category = ""
                                    else category = cat
                                }
                            ) {
                                Text(
                                    cat,
                                    color = if (isSelected) VaultBlack else Color.White,
                                    fontSize = 12.sp,
                                    fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)
                                )
                            }
                        }
                    }
                    if (category.isEmpty()) {
                        OutlinedTextField(
                            value = customCategory,
                            onValueChange = { customCategory = it; category = it },
                            label = { Text("New Category Name *") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                            colors = fieldColors
                        )
                    }
                } else {
                    OutlinedTextField(
                        value = customCategory,
                        onValueChange = { customCategory = it; category = it },
                        label = { Text("Category *") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                        colors = fieldColors
                    )
                }
                OutlinedTextField(
                    value = qrCodeInput,
                    onValueChange = { qrCodeInput = it },
                    label = { Text("Barcode / QR Code (optional)") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = fieldColors,
                    leadingIcon = { Icon(Icons.Default.QrCode, null, tint = Color.Gray, modifier = Modifier.size(18.dp)) },
                    placeholder = { Text("Leave blank to auto-generate", color = Color(0xFF555555), fontSize = 11.sp) },
                    trailingIcon = {
                        IconButton(onClick = onScanQrCode) {
                            Icon(
                                Icons.Default.QrCodeScanner,
                                contentDescription = "Scan QR",
                                tint = Gold,
                                modifier = Modifier.size(22.dp)
                            )
                        }
                    }
                )
                if (qrCodeInput.isNotBlank()) {
                    Text("Scanned: $qrCodeInput", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                }
                if (!canConfirm && name.isNotBlank()) {
                    Text("Fill in all required fields (*)", color = Color(0xFF888888), fontSize = 11.sp)
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(name.trim(), price, category.trim(), qrCodeInput.trim()) },
                enabled = canConfirm,
                colors = ButtonDefaults.buttonColors(containerColor = Gold, disabledContainerColor = Color(0xFF2A2A2A)),
                shape = RoundedCornerShape(10.dp)
            ) {
                Icon(Icons.Default.Save, null, tint = VaultBlack, modifier = Modifier.size(16.dp))
                Spacer(Modifier.width(6.dp))
                Text("SAVE PRODUCT", color = VaultBlack, fontWeight = FontWeight.Black)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", color = Color.Gray)
            }
        }
    )
}

// ── QUICK SALE CHIP ────────────────────────────────────────────────────────────
@Composable
fun QuickSaleChip(
    preset: QuickSalePreset,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    val icon = when (preset.icon) {
        "style"        -> Icons.Default.Style
        "stars"        -> Icons.Default.Stars
        "shopping_bag" -> Icons.Default.ShoppingBag
        else           -> Icons.Default.Add
    }
    Surface(
        modifier = modifier
            .clip(RoundedCornerShape(8.dp))
            .clickable(onClick = onClick),
        color = Color(0xFF1A1A1A),
        shape = RoundedCornerShape(8.dp),
        tonalElevation = 0.dp
    ) {
        Column(
            Modifier.padding(vertical = 8.dp, horizontal = 4.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Icon(icon, null, tint = Gold, modifier = Modifier.size(18.dp))
            Spacer(Modifier.height(3.dp))
            Text(
                preset.label,
                color = Color.White,
                fontSize = 9.sp,
                fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Center,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}

// ── QUICK SALE AMOUNT DIALOG ───────────────────────────────────────────────────
@Composable
fun QuickSaleAmountDialog(
    preset: QuickSalePreset,
    onConfirm: (Double) -> Unit,
    onDismiss: () -> Unit
) {
    var entry by remember { mutableStateOf("") }

    fun display() = if (entry.isEmpty()) "$0.00" else "$$entry"
    fun append(ch: String) {
        if (ch == "." && entry.contains(".")) return
        val parts = entry.split(".")
        if (parts.size == 2 && parts[1].length >= 2) return
        if (entry.isEmpty() && ch == "0") return
        entry += ch
    }
    fun backspace() { if (entry.isNotEmpty()) entry = entry.dropLast(1) }
    fun parsed() = entry.toDoubleOrNull() ?: 0.0

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = VaultSurface,
        shape = RoundedCornerShape(16.dp),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                val icon = when (preset.icon) {
                    "style"        -> Icons.Default.Style
                    "stars"        -> Icons.Default.Stars
                    "shopping_bag" -> Icons.Default.ShoppingBag
                    else           -> Icons.Default.Add
                }
                Icon(icon, null, tint = Gold, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text(preset.label, color = Color.White, fontWeight = FontWeight.Black, fontSize = 16.sp)
            }
        },
        text = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                // Amount display
                Surface(
                    color = Color(0xFF111111),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        display(),
                        color = Gold,
                        fontSize = 36.sp,
                        fontWeight = FontWeight.Black,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(vertical = 16.dp)
                    )
                }
                Spacer(Modifier.height(16.dp))
                // Number pad
                val keys = listOf("1","2","3","4","5","6","7","8","9",".","0","⌫")
                LazyVerticalGrid(
                    columns = GridCells.Fixed(3),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.height(200.dp),
                    userScrollEnabled = false
                ) {
                    items(keys) { key ->
                        Box(
                            Modifier
                                .height(44.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .background(if (key == "⌫") Color(0xFF2A1A1A) else Color(0xFF2A2A2A))
                                .clickable { if (key == "⌫") backspace() else append(key) },
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                key,
                                color = if (key == "⌫") Color(0xFFE57373) else Color.White,
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { if (parsed() > 0) onConfirm(parsed()) },
                enabled = parsed() > 0,
                colors = ButtonDefaults.buttonColors(containerColor = Gold, disabledContainerColor = Color(0xFF333333)),
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(10.dp)
            ) {
                Icon(Icons.Default.AddShoppingCart, null, tint = VaultBlack, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp))
                Text("ADD TO ORDER", color = VaultBlack, fontWeight = FontWeight.Black)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", color = Color.Gray, fontSize = 12.sp)
            }
        }
    )
}

// ── CART ITEM PRICE EDIT DIALOG ───────────────────────────────────────────────
@Composable
fun CartItemPriceDialog(
    item: CartItemEntity,
    onConfirm: (Double) -> Unit,
    onDismiss: () -> Unit
) {
    var entry by remember { mutableStateOf("%.2f".format(item.price)) }
    // Clear prefilled value on first digit press so the user types a fresh price
    var freshEntry by remember { mutableStateOf(true) }

    fun display() = "$$entry"
    fun append(ch: String) {
        if (freshEntry && ch != "⌫") {
            entry = if (ch == ".") "0." else ch
            freshEntry = false
            return
        }
        if (ch == "." && entry.contains(".")) return
        val parts = entry.split(".")
        if (parts.size == 2 && parts[1].length >= 2) return
        if (entry == "0" && ch != ".") { entry = ch; return }
        entry += ch
    }
    fun backspace() {
        freshEntry = false
        if (entry.isNotEmpty()) entry = entry.dropLast(1)
    }
    fun parsed() = entry.toDoubleOrNull() ?: 0.0

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = VaultSurface,
        shape = RoundedCornerShape(16.dp),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Edit, null, tint = Gold, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text(
                    item.name,
                    color = Color.White,
                    fontWeight = FontWeight.Black,
                    fontSize = 16.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        },
        text = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Surface(
                    color = Color(0xFF111111),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        display(),
                        color = Gold,
                        fontSize = 36.sp,
                        fontWeight = FontWeight.Black,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(vertical = 16.dp)
                    )
                }
                Spacer(Modifier.height(16.dp))
                val keys = listOf("1","2","3","4","5","6","7","8","9",".","0","⌫")
                LazyVerticalGrid(
                    columns = GridCells.Fixed(3),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.height(200.dp),
                    userScrollEnabled = false
                ) {
                    items(keys) { key ->
                        Box(
                            Modifier
                                .height(44.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .background(if (key == "⌫") Color(0xFF2A1A1A) else Color(0xFF2A2A2A))
                                .clickable { if (key == "⌫") backspace() else append(key) },
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                key,
                                color = if (key == "⌫") Color(0xFFE57373) else Color.White,
                                fontSize = 18.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { if (parsed() > 0) onConfirm(parsed()) },
                enabled = parsed() > 0,
                colors = ButtonDefaults.buttonColors(containerColor = Gold, disabledContainerColor = Color(0xFF333333)),
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(10.dp)
            ) {
                Icon(Icons.Default.Check, null, tint = VaultBlack, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp))
                Text("SET PRICE", color = VaultBlack, fontWeight = FontWeight.Black)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", color = Color.Gray, fontSize = 12.sp)
            }
        }
    )
}

// ── CART ITEM DISCOUNT DIALOG ─────────────────────────────────────────────────
@Composable
fun CartItemDiscountDialog(
    item: CartItemEntity,
    currentDiscountPct: Double = 0.0,
    onApply: (Double) -> Unit,
    onDismiss: () -> Unit
) {
    var customEntry by remember { mutableStateOf(if (currentDiscountPct > 0) currentDiscountPct.toInt().toString() else "") }
    val quickOptions = listOf(10.0, 15.0, 20.0, 25.0, 50.0)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = VaultSurface,
        shape = RoundedCornerShape(16.dp),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.LocalOffer, null, tint = Color(0xFFFF9800), modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text(
                    item.name,
                    color = Color.White,
                    fontWeight = FontWeight.Black,
                    fontSize = 15.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Quick discount", color = Color(0xFF888888), fontSize = 12.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    quickOptions.forEach { pct ->
                        val isActive = customEntry == pct.toInt().toString()
                        Surface(
                            onClick = { customEntry = pct.toInt().toString() },
                            color = if (isActive) Color(0xFFFF9800).copy(alpha = 0.18f) else Color(0xFF2A2A2A),
                            shape = RoundedCornerShape(8.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text(
                                "${pct.toInt()}%",
                                color = if (isActive) Color(0xFFFF9800) else Color.White,
                                fontWeight = if (isActive) FontWeight.Bold else FontWeight.Normal,
                                fontSize = 13.sp,
                                textAlign = TextAlign.Center,
                                modifier = Modifier.padding(vertical = 8.dp)
                            )
                        }
                    }
                }
                Spacer(Modifier.height(4.dp))
                Text("Custom %", color = Color(0xFF888888), fontSize = 12.sp)
                OutlinedTextField(
                    value = customEntry,
                    onValueChange = { v ->
                        val cleaned = v.filter { it.isDigit() }
                        if (cleaned.isEmpty() || (cleaned.toIntOrNull() ?: 0) <= 100) customEntry = cleaned
                    },
                    placeholder = { Text("e.g. 30", color = Color(0xFF555555)) },
                    suffix = { Text("%", color = Color(0xFF888888)) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Color(0xFFFF9800),
                        unfocusedBorderColor = Color(0xFF444444),
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White
                    ),
                    modifier = Modifier.fillMaxWidth()
                )
                if (currentDiscountPct > 0) {
                    TextButton(
                        onClick = { onApply(0.0) },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Remove existing discount", color = Color(0xFFEF5350), fontSize = 12.sp)
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    val pct = customEntry.toDoubleOrNull() ?: 0.0
                    if (pct > 0) onApply(pct) else onDismiss()
                },
                enabled = (customEntry.toDoubleOrNull() ?: 0.0) > 0,
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFFF9800), disabledContainerColor = Color(0xFF333333)),
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(10.dp)
            ) {
                Text("APPLY DISCOUNT", color = Color.Black, fontWeight = FontWeight.Black)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("CANCEL", color = Color.Gray, fontSize = 12.sp)
            }
        }
    )
}

// ── MARKET PRICE DETAIL DIALOG ───────────────────────────────────────────────
@Composable
fun MarketPriceDetailDialog(
    item: CartItemEntity,
    result: MarketPriceResult,
    storePrice: Double,
    onDismiss: () -> Unit
) {
    val badgeColor = when (result.price_badge) {
        "HIGH" -> Color(0xFFEF5350)
        "LOW"  -> Color(0xFF66BB6A)
        else   -> Color(0xFF26C6DA)
    }
    val confidenceColor = when (result.confidence) {
        "HIGH"   -> Color(0xFF66BB6A)
        "MEDIUM" -> Color(0xFFFFB300)
        else     -> Color(0xFF888888)
    }
    val variance = if (result.weighted_avg > 0) ((storePrice - result.weighted_avg) / result.weighted_avg * 100) else 0.0

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(0xFF1A1A1A),
        tonalElevation = 0.dp,
        shape = RoundedCornerShape(16.dp),
        title = {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(item.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.clip(RoundedCornerShape(4.dp)).background(badgeColor.copy(alpha = 0.15f)).padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text(result.price_badge, color = badgeColor, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                    Box(
                        Modifier.clip(RoundedCornerShape(4.dp)).background(confidenceColor.copy(alpha = 0.12f)).padding(horizontal = 6.dp, vertical = 2.dp)
                    ) {
                        Text("${result.confidence} CONFIDENCE", color = confidenceColor, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                    if (result.from_cache) {
                        Text("cached", color = Color(0xFF555555), fontSize = 10.sp)
                    }
                }
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                // Price comparison
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column {
                        Text("YOUR PRICE", color = Color(0xFF666666), fontSize = 10.sp)
                        Text("${"%.2f".format(storePrice)}", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 20.sp)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("MARKET AVG", color = Color(0xFF666666), fontSize = 10.sp)
                        Text("${"%.2f".format(result.weighted_avg)}", color = badgeColor, fontWeight = FontWeight.Bold, fontSize = 20.sp)
                    }
                }
                val sign = if (variance >= 0) "+" else ""
                Text(
                    "${sign}${"%.1f".format(variance)}% vs market  •  ${result.total_samples} samples",
                    color = if (variance > 10) Color(0xFFEF5350) else if (variance < -10) Color(0xFF66BB6A) else Color(0xFF888888),
                    fontSize = 11.sp
                )

                HorizontalDivider(color = Color(0xFF2A2A2A))

                // Secondary metrics
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column {
                        Text("BUY PRICE (50%)", color = Color(0xFF555555), fontSize = 10.sp)
                        Text("${"%.2f".format(result.buy_price)}", color = Gold, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("TRADE VALUE (80%)", color = Color(0xFF555555), fontSize = 10.sp)
                        Text("${"%.2f".format(result.trade_value)}", color = Color(0xFF26C6DA), fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                    }
                }

                // Sources
                if (result.sources.pokemontcg.count > 0 || result.sources.ebay.count > 0 || result.sources.local.count > 0) {
                    HorizontalDivider(color = Color(0xFF2A2A2A))
                    Text("SOURCES", color = Color(0xFF555555), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        if (result.sources.pokemontcg.count > 0) {
                            MarketSourceRow("TCGPlayer", result.sources.pokemontcg.avg, result.sources.pokemontcg.count)
                        }
                        if (result.sources.ebay.count > 0) {
                            MarketSourceRow("eBay Last 3 Sold", result.sources.ebay.avg, result.sources.ebay.count)
                            if (result.sources.ebay.prices.isNotEmpty()) {
                                Row(Modifier.padding(start = 12.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                    result.sources.ebay.prices.forEachIndexed { idx, p ->
                                        Surface(color = Color(0xFFE53935).copy(alpha = 0.1f), shape = RoundedCornerShape(4.dp)) {
                                            Text("#${idx + 1}: $${String.format("%.2f", p)}", color = Color(0xFFE53935), fontSize = 9.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                        }
                                    }
                                }
                            }
                        }
                        if (result.sources.local.count > 0) {
                            MarketSourceRow("Local sales (30d)", result.sources.local.avg, result.sources.local.count)
                        }
                    }
                }

                // AI insight
                val insight = result.ai_insight
                if (!insight.isNullOrBlank() && insight.length > 5) {
                    HorizontalDivider(color = Color(0xFF2A2A2A))
                    Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Icon(Icons.Default.AutoAwesome, null, tint = Gold, modifier = Modifier.size(14.dp).padding(top = 1.dp))
                        Text(insight, color = Color(0xFFAAAAAA), fontSize = 11.sp, lineHeight = 16.sp)
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text("CLOSE", color = Gold, fontWeight = FontWeight.Bold)
            }
        }
    )
}

@Composable
private fun MarketSourceRow(label: String, avg: Double, count: Int) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, color = Color(0xFF777777), fontSize = 11.sp)
        Text("${"%.2f".format(avg)}  ($count listings)", color = Color(0xFF999999), fontSize = 11.sp)
    }
}

@Composable
fun EstimateBreakdownDialog(
    item: CartItemEntity,
    result: MarketPriceResult,
    onDismiss: () -> Unit
) {
    val marketVal = result.weighted_avg
    val tradeVal = result.trade_value
    val cashOffer = result.buy_price
    val storePrice = item.price

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(0xFF111111),
        tonalElevation = 0.dp,
        shape = RoundedCornerShape(16.dp),
        title = {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("PRICE ESTIMATE", color = Color(0xFF00E676), fontWeight = FontWeight.Bold, fontSize = 11.sp, letterSpacing = 1.5.sp)
                Text(item.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 15.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    val confColor = when (result.confidence) {
                        "HIGH" -> Color(0xFF66BB6A)
                        "MEDIUM" -> Color(0xFFFFB300)
                        else -> Color(0xFF888888)
                    }
                    Box(Modifier.clip(RoundedCornerShape(4.dp)).background(confColor.copy(alpha = 0.12f)).padding(horizontal = 6.dp, vertical = 2.dp)) {
                        Text("${result.confidence} CONFIDENCE", color = confColor, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                    }
                    Text("${result.total_samples} samples", color = Color(0xFF555555), fontSize = 9.sp)
                }
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(0.dp)) {
                EstimateRow(label = "MARKET VALUE", sublabel = "What it sells for online", value = marketVal, color = Color(0xFF26C6DA), isLarge = true)
                HorizontalDivider(color = Color(0xFF1E1E1E), modifier = Modifier.padding(vertical = 6.dp))
                EstimateRow(label = "TRADE-IN VALUE", sublabel = "Store credit we offer", value = tradeVal, color = Color(0xFF66BB6A), isLarge = true)
                HorizontalDivider(color = Color(0xFF1E1E1E), modifier = Modifier.padding(vertical = 6.dp))
                EstimateRow(label = "CASH OFFER", sublabel = "What we pay in cash", value = cashOffer, color = Gold, isLarge = true)
                HorizontalDivider(color = Color(0xFF1E1E1E), modifier = Modifier.padding(vertical = 6.dp))
                EstimateRow(label = "YOUR PRICE", sublabel = "What you're paying today", value = storePrice, color = Color.White, isLarge = false)

                Spacer(Modifier.height(10.dp))
                Box(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(Color(0xFF00E676).copy(alpha = 0.06f)).padding(12.dp)
                ) {
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text("HOW WE PRICE", color = Color(0xFF00E676), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                        Text(
                            "Market value is the average of TCGPlayer listings and recent sales. " +
                            "Trade-in is based on our selling history. Cash offer reflects our standard buylist rate.",
                            color = Color(0xFF888888),
                            fontSize = 10.sp,
                            lineHeight = 15.sp
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text("GOT IT", color = Color(0xFF00E676), fontWeight = FontWeight.Bold)
            }
        }
    )
}

@Composable
private fun EstimateRow(label: String, sublabel: String, value: Double, color: Color, isLarge: Boolean) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
        Column {
            Text(label, color = color.copy(alpha = 0.7f), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp)
            Text(sublabel, color = Color(0xFF555555), fontSize = 9.sp)
        }
        Text(
            "$${"%.2f".format(value)}",
            color = color,
            fontSize = if (isLarge) 20.sp else 15.sp,
            fontWeight = FontWeight.Bold
        )
    }
}

// ── CARD DECLINED DIALOG ─────────────────────────────────────────────────────
@Composable
fun CardDeclinedDialog(onDismiss: () -> Unit) {
    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.88f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(380.dp).clickable(onClick = {}),
            shape = RoundedCornerShape(20.dp),
            color = Color(0xFF1A0808)
        ) {
            Column(Modifier.padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Box(
                    Modifier.size(64.dp).clip(CircleShape).background(Color(0xFF4A0000)),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(Icons.Default.CreditCardOff, null, tint = Color(0xFFEF5350), modifier = Modifier.size(32.dp))
                }
                Text("CARD DECLINED", fontWeight = FontWeight.Black, color = Color(0xFFEF5350), fontSize = 18.sp, letterSpacing = 2.sp)
                Text(
                    "The card was declined by the payment terminal.\nPlease try a different card or another payment method.",
                    color = Color(0xFFAAAAAA),
                    fontSize = 13.sp,
                    lineHeight = 18.sp,
                    textAlign = androidx.compose.ui.text.style.TextAlign.Center
                )
                Spacer(Modifier.height(4.dp))
                Button(
                    onClick = onDismiss,
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A0000))
                ) {
                    Text("TRY AGAIN", fontWeight = FontWeight.Black, color = Color.White, letterSpacing = 1.sp)
                }
            }
        }
    }
}

// ── SALES HISTORY PANEL ──────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SalesHistoryPanel(
    sales: List<SaleEntity>,
    consignors: List<ConsignorEntity> = emptyList(),
    consignmentItems: List<ConsignmentItemEntity> = emptyList(),
    onRefund: (Int) -> Unit,
    onIntent: (POSIntent) -> Unit = {},
    onDismiss: () -> Unit
) {
    val gold = Gold
    val tradePayMethods = setOf("TRADE_CASH", "TRADE_CREDIT")
    val inventorySales = remember(sales) { sales.filter { it.paymentMethod.uppercase() !in tradePayMethods } }
    val tradeInSales   = remember(sales) { sales.filter { it.paymentMethod.uppercase() in tradePayMethods } }
    val soldConsignmentItems = remember(consignmentItems) { consignmentItems.filter { it.status == "SOLD" } }

    var selectedTab by remember { mutableStateOf(0) }  // 0=All  1=Inventory  2=Trade-In  3=Consignment
    val tabSales = when (selectedTab) { 1 -> inventorySales; 2 -> tradeInSales; else -> sales }

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.85f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(640.dp).heightIn(max = 820.dp).clickable(onClick = {}),
            shape = RoundedCornerShape(20.dp),
            color = Color(0xFF111111)
        ) {
            Column(Modifier.fillMaxSize()) {
                // Header
                Row(
                    Modifier.fillMaxWidth().background(Color(0xFF1A1A1A)).padding(horizontal = 20.dp, vertical = 16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Receipt, null, tint = gold, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(10.dp))
                        Text("COMPLETED SALES", fontWeight = FontWeight.Black, color = gold, fontSize = 14.sp, letterSpacing = 2.sp)
                    }
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Default.Close, null, tint = Color.Gray)
                    }
                }

                // ── Tab strip ──────────────────────────────────────────────────────────
                val tabs = listOf(
                    Triple("ALL", sales.size, gold),
                    Triple("INVENTORY", inventorySales.size, Color(0xFF4ADE80)),
                    Triple("TRADE-INS", tradeInSales.size, Color(0xFFF59E0B)),
                    Triple("CONSIGNMENT", soldConsignmentItems.size, Color(0xFFE879F9))
                )
                Row(Modifier.fillMaxWidth().background(Color(0xFF161616))) {
                    tabs.forEachIndexed { idx, (label, count, tint) ->
                        val active = selectedTab == idx
                        Column(
                            Modifier.weight(1f).clickable { selectedTab = idx }
                                .background(if (active) Color(0xFF1E1E1E) else Color.Transparent)
                                .padding(vertical = 10.dp),
                            horizontalAlignment = Alignment.CenterHorizontally
                        ) {
                            Text(label, color = if (active) tint else Color(0xFF555555),
                                fontSize = 10.sp, fontWeight = if (active) FontWeight.Black else FontWeight.Normal,
                                letterSpacing = 1.sp)
                            Text("$count", color = if (active) tint else Color(0xFF444444),
                                fontSize = 9.sp, fontWeight = FontWeight.Bold)
                        }
                        if (idx < tabs.lastIndex) VerticalDivider(modifier = Modifier.height(38.dp).align(Alignment.CenterVertically), color = Color(0xFF2A2A2A))
                    }
                }

                HorizontalDivider(color = Color(0xFF2A2A2A))

                // ── Consignment tab ───────────────────────────────────────────────────
                if (selectedTab == 3) {
                    ConsignmentPanel(
                        consignors = consignors,
                        consignmentItems = consignmentItems,
                        onIntent = onIntent
                    )
                } else {

                // ── Summary bar ────────────────────────────────────────────────────────
                if (tabSales.isNotEmpty()) {
                    val totalAmt = tabSales.filter { !it.isRefunded }.sumOf { it.totalAmount }
                    val refunded = tabSales.count { it.isRefunded }
                    val tint = tabs[selectedTab].third
                    Row(
                        Modifier.fillMaxWidth().background(Color(0xFF141414)).padding(horizontal = 20.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("${tabSales.size} transaction${if (tabSales.size != 1) "s" else ""}",
                            color = Color(0xFF666666), fontSize = 11.sp, modifier = Modifier.weight(1f))
                        if (refunded > 0) {
                            Text("$refunded refunded  •  ", color = Color(0xFF884444), fontSize = 10.sp)
                        }
                        Text("Total  $${String.format("%.2f", totalAmt)}", color = tint, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                    }
                }

                if (tabSales.isEmpty()) {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Icon(Icons.Default.Receipt, null, tint = Color(0xFF2A2A2A), modifier = Modifier.size(48.dp))
                            Spacer(Modifier.height(8.dp))
                            val emptyMsg = when (selectedTab) {
                                1 -> "No inventory sales yet"
                                2 -> "No trade-ins recorded yet"
                                else -> "No completed sales yet"
                            }
                            Text(emptyMsg, color = Color(0xFF444444), fontSize = 13.sp)
                        }
                    }
                } else {
                    LazyColumn(
                        Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(16.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        items(tabSales, key = { it.id }) { sale ->
                            val isTradeIn = sale.paymentMethod.uppercase() in tradePayMethods
                            val sdf = java.text.SimpleDateFormat("MMM d  h:mm a", java.util.Locale.getDefault())
                            val timeStr = sdf.format(java.util.Date(sale.timestamp))
                            val cardBg = if (isTradeIn) Color(0xFF1A1500) else Color(0xFF1A1A1A)
                            Surface(color = cardBg, shape = RoundedCornerShape(12.dp)) {
                                Column(Modifier.padding(14.dp)) {
                                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.weight(1f)) {
                                            // Payment badge
                                            val (badgeColor, badgeText) = when (sale.paymentMethod.uppercase()) {
                                                "CARD" -> Color(0xFF0A4D82) to "CARD"
                                                "CASH" -> Color(0xFF1A3A1A) to "CASH"
                                                "STORE_CREDIT", "STORECREDIT" -> Color(0xFF2D1A4A) to "CREDIT"
                                                "PAYPAL" -> Color(0xFF003087) to "PAYPAL"
                                                "VENMO" -> Color(0xFF008CFF) to "VENMO"
                                                "CASH_APP" -> Color(0xFF00A827) to "CASH APP"
                                                "TRADE_CASH" -> Color(0xFF3A2A00) to "TRADE CASH"
                                                "TRADE_CREDIT" -> Color(0xFF1A2A3A) to "TRADE CREDIT"
                                                else -> Color(0xFF2A2A2A) to sale.paymentMethod.take(10).uppercase()
                                            }
                                            Surface(color = badgeColor, shape = RoundedCornerShape(6.dp)) {
                                                Text(badgeText, color = Color.White, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp, modifier = Modifier.padding(horizontal = 6.dp, vertical = 3.dp))
                                            }
                                            if (sale.isRefunded) {
                                                Spacer(Modifier.width(6.dp))
                                                Surface(color = Color(0xFF3D1A1A), shape = RoundedCornerShape(6.dp)) {
                                                    Text("REFUNDED", color = Color(0xFFEF5350), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp, modifier = Modifier.padding(horizontal = 6.dp, vertical = 3.dp))
                                                }
                                            }
                                            Spacer(Modifier.width(10.dp))
                                            Text(timeStr, color = Color.Gray, fontSize = 11.sp)
                                        }
                                        Text("$${String.format("%.2f", sale.totalAmount)}", fontWeight = FontWeight.Black, color = Gold, fontSize = 18.sp)
                                    }
                                    // Items preview
                                    if (sale.items.isNotEmpty()) {
                                        Spacer(Modifier.height(6.dp))
                                        val preview = sale.items.take(3).joinToString(" · ") + if (sale.items.size > 3) " +${sale.items.size - 3} more" else ""
                                        Text(preview, color = Color(0xFF777777), fontSize = 11.sp, maxLines = 1)
                                    }
                                    // Refund button
                                    if (!sale.isRefunded) {
                                        Spacer(Modifier.height(10.dp))
                                        HorizontalDivider(color = Color(0xFF2A2A2A))
                                        Spacer(Modifier.height(8.dp))
                                        var confirmRefund by remember { mutableStateOf(false) }
                                        if (!confirmRefund) {
                                            OutlinedButton(
                                                onClick = { confirmRefund = true },
                                                modifier = Modifier.fillMaxWidth().height(36.dp),
                                                shape = RoundedCornerShape(8.dp),
                                                colors = ButtonDefaults.outlinedButtonColors(contentColor = Color(0xFFEF5350)),
                                                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF4A1A1A))
                                            ) {
                                                Icon(Icons.Default.Undo, null, tint = Color(0xFFEF5350), modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(6.dp))
                                                Text("REFUND", fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                            }
                                        } else {
                                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                                OutlinedButton(
                                                    onClick = { confirmRefund = false },
                                                    modifier = Modifier.weight(1f).height(36.dp),
                                                    shape = RoundedCornerShape(8.dp),
                                                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.Gray),
                                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF2A2A2A))
                                                ) { Text("CANCEL", fontSize = 11.sp) }
                                                Button(
                                                    onClick = { onRefund(sale.id); confirmRefund = false },
                                                    modifier = Modifier.weight(1f).height(36.dp),
                                                    shape = RoundedCornerShape(8.dp),
                                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A0000))
                                                ) {
                                                    Icon(Icons.Default.Undo, null, tint = Color.White, modifier = Modifier.size(14.dp))
                                                    Spacer(Modifier.width(4.dp))
                                                    Text("CONFIRM REFUND", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color.White)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                } // end else (non-consignment tabs)
            }
        }
    }
}

// ── CONSIGNMENT PANEL ─────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConsignmentPanel(
    consignors: List<ConsignorEntity>,
    consignmentItems: List<ConsignmentItemEntity>,
    onIntent: (POSIntent) -> Unit
) {
    val purple = Color(0xFFE879F9)
    val amber  = Color(0xFFF59E0B)

    var showAddConsignor by remember { mutableStateOf(false) }
    var expandedConsignorId by remember { mutableStateOf<Long?>(null) }
    var showAddItemFor by remember { mutableStateOf<ConsignorEntity?>(null) }

    val activeItems  = remember(consignmentItems) { consignmentItems.filter { it.status == "ACTIVE" } }
    val soldItems    = remember(consignmentItems) { consignmentItems.filter { it.status == "SOLD" } }
    val totalValue   = activeItems.sumOf { it.askingPrice }
    val totalPayout  = soldItems.sumOf { it.payoutAmount }

    Column(Modifier.fillMaxSize()) {
        // Summary bar
        Row(
            Modifier.fillMaxWidth().background(Color(0xFF150A1A)).padding(horizontal = 16.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text("${activeItems.size} active  •  ${soldItems.size} sold", color = Color(0xFF888888), fontSize = 11.sp)
                Text("Active value  $${String.format("%.2f", totalValue)}", color = purple, fontSize = 12.sp, fontWeight = FontWeight.Bold)
            }
            Column(horizontalAlignment = Alignment.End) {
                Text("Payout owed", color = Color(0xFF888888), fontSize = 10.sp)
                Text("$${String.format("%.2f", totalPayout)}", color = amber, fontSize = 14.sp, fontWeight = FontWeight.Black)
            }
        }

        // Add consignor button
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("CONSIGNORS", color = Color(0xFF666666), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
            Button(
                onClick = { showAddConsignor = true },
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2D0A3A)),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                modifier = Modifier.height(32.dp)
            ) {
                Icon(Icons.Default.Add, null, tint = purple, modifier = Modifier.size(14.dp))
                Spacer(Modifier.width(4.dp))
                Text("Add Consignor", color = purple, fontSize = 11.sp, fontWeight = FontWeight.Bold)
            }
        }

        if (consignors.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(Icons.Default.Person, null, tint = Color(0xFF2A2A2A), modifier = Modifier.size(48.dp))
                    Spacer(Modifier.height(8.dp))
                    Text("No consignors yet", color = Color(0xFF444444), fontSize = 13.sp)
                    Spacer(Modifier.height(4.dp))
                    Text("Tap 'Add Consignor' to get started", color = Color(0xFF333333), fontSize = 11.sp)
                }
            }
        } else {
            LazyColumn(
                Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(consignors, key = { it.id }) { consignor ->
                    val myItems = remember(consignmentItems, consignor.id) {
                        consignmentItems.filter { it.consignorId == consignor.id }
                    }
                    val myActive = myItems.filter { it.status == "ACTIVE" }
                    val mySold   = myItems.filter { it.status == "SOLD" }
                    val isExpanded = expandedConsignorId == consignor.id
                    var confirmDelete by remember { mutableStateOf(false) }

                    Surface(color = Color(0xFF1A0A22), shape = RoundedCornerShape(12.dp)) {
                        Column(Modifier.fillMaxWidth()) {
                            // Consignor row header
                            Row(
                                Modifier.fillMaxWidth().clickable {
                                    expandedConsignorId = if (isExpanded) null else consignor.id
                                }.padding(14.dp),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Column(Modifier.weight(1f)) {
                                    Text(consignor.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                        Text("${myActive.size} active", color = purple, fontSize = 11.sp)
                                        Text("${mySold.size} sold", color = Color(0xFF888888), fontSize = 11.sp)
                                        if (consignor.phone.isNotBlank()) Text(consignor.phone, color = Color(0xFF666666), fontSize = 10.sp)
                                    }
                                }
                                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Text("${(consignor.payoutRate * 100).toInt()}%", color = amber, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    Icon(
                                        if (isExpanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                                        null, tint = Color(0xFF666666), modifier = Modifier.size(20.dp)
                                    )
                                }
                            }

                            // Expanded section
                            if (isExpanded) {
                                HorizontalDivider(color = Color(0xFF2A0A3A))
                                Column(Modifier.fillMaxWidth().padding(14.dp)) {
                                    // Items for this consignor
                                    myItems.forEach { item ->
                                        val isSold = item.status == "SOLD"
                                        Surface(
                                            color = if (isSold) Color(0xFF0D0D0D) else Color(0xFF100820),
                                            shape = RoundedCornerShape(8.dp),
                                            modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp)
                                        ) {
                                            Row(
                                                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                                                horizontalArrangement = Arrangement.SpaceBetween,
                                                verticalAlignment = Alignment.CenterVertically
                                            ) {
                                                Column(Modifier.weight(1f)) {
                                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                                        if (isSold) Surface(color = Color(0xFF0A2A0A), shape = RoundedCornerShape(4.dp)) {
                                                            Text("SOLD", color = Color(0xFF4ADE80), fontSize = 8.sp, fontWeight = FontWeight.Black, modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp))
                                                        }
                                                        Text(item.cardName, color = if (isSold) Color(0xFF666666) else Color.White, fontWeight = FontWeight.Medium, fontSize = 13.sp)
                                                    }
                                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                                        if (item.setName.isNotBlank()) Text(item.setName, color = Color(0xFF555555), fontSize = 10.sp)
                                                        Text(item.condition, color = Color(0xFF555555), fontSize = 10.sp)
                                                    }
                                                }
                                                Column(horizontalAlignment = Alignment.End) {
                                                    Text("$${String.format("%.2f", item.askingPrice)}", color = if (isSold) Color(0xFF555555) else Color.White, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                                                    Text("Payout $${String.format("%.2f", item.payoutAmount)}", color = amber, fontSize = 10.sp)
                                                }
                                                Spacer(Modifier.width(8.dp))
                                                if (!isSold) {
                                                    IconButton(onClick = { onIntent(POSIntent.MarkConsignmentSold(item.id)) }, modifier = Modifier.size(32.dp)) {
                                                        Icon(Icons.Default.CheckCircle, "Mark sold", tint = Color(0xFF4ADE80), modifier = Modifier.size(18.dp))
                                                    }
                                                }
                                                IconButton(onClick = { onIntent(POSIntent.RemoveConsignmentItem(item)) }, modifier = Modifier.size(32.dp)) {
                                                    Icon(Icons.Default.Delete, "Remove", tint = Color(0xFF4A1A1A), modifier = Modifier.size(16.dp))
                                                }
                                            }
                                        }
                                    }
                                    Spacer(Modifier.height(8.dp))
                                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        OutlinedButton(
                                            onClick = { showAddItemFor = consignor },
                                            modifier = Modifier.weight(1f).height(36.dp),
                                            colors = ButtonDefaults.outlinedButtonColors(contentColor = purple),
                                            border = BorderStroke(1.dp, purple.copy(alpha = 0.4f)),
                                            shape = RoundedCornerShape(8.dp)
                                        ) {
                                            Icon(Icons.Default.Add, null, modifier = Modifier.size(14.dp))
                                            Spacer(Modifier.width(4.dp))
                                            Text("Add Card", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                        }
                                        if (!confirmDelete) {
                                            OutlinedButton(
                                                onClick = { confirmDelete = true },
                                                modifier = Modifier.weight(1f).height(36.dp),
                                                colors = ButtonDefaults.outlinedButtonColors(contentColor = Color(0xFF666666)),
                                                border = BorderStroke(1.dp, Color(0xFF2A2A2A)),
                                                shape = RoundedCornerShape(8.dp)
                                            ) { Text("Remove Consignor", fontSize = 11.sp) }
                                        } else {
                                            Button(
                                                onClick = { onIntent(POSIntent.DeleteConsignor(consignor)); confirmDelete = false },
                                                modifier = Modifier.weight(1f).height(36.dp),
                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A0000)),
                                                shape = RoundedCornerShape(8.dp)
                                            ) { Text("Confirm Delete", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color.White) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Add Consignor dialog ────────────────────────────────────────────────────
    if (showAddConsignor) {
        var nameInput by remember { mutableStateOf("") }
        var phoneInput by remember { mutableStateOf("") }
        var notesInput by remember { mutableStateOf("") }
        var payoutInput by remember { mutableStateOf("70") }

        AlertDialog(
            onDismissRequest = { showAddConsignor = false },
            containerColor = Color(0xFF1A1A1A),
            shape = RoundedCornerShape(16.dp),
            title = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.Person, null, tint = purple, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("New Consignor", color = Color.White, fontWeight = FontWeight.Bold)
                }
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(value = nameInput, onValueChange = { nameInput = it },
                        label = { Text("Name *", color = Color(0xFF888888)) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White),
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = phoneInput, onValueChange = { phoneInput = it },
                        label = { Text("Phone (optional)", color = Color(0xFF888888)) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White),
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = payoutInput, onValueChange = { payoutInput = it },
                        label = { Text("Payout % (e.g. 70)", color = Color(0xFF888888)) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White),
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(
                        value = notesInput,
                        onValueChange = { notesInput = it },
                        label = { Text("Notes (optional)", color = Color(0xFF888888)) },
                        singleLine = false,
                        minLines = 5,
                        maxLines = 10,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = purple,
                            unfocusedBorderColor = Color(0xFF333333),
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White
                        ),
                        placeholder = { Text("Card conditions, special instructions, contact preferences, agreed terms…", color = Color(0xFF444444), fontSize = 11.sp) },
                        modifier = Modifier.fillMaxWidth().heightIn(min = 120.dp)
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        if (nameInput.isNotBlank()) {
                            val rate = (payoutInput.toDoubleOrNull() ?: 70.0) / 100.0
                            onIntent(POSIntent.AddConsignor(nameInput.trim(), phoneInput.trim(), notesInput.trim(), rate))
                            showAddConsignor = false
                        }
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2D0A3A))
                ) { Text("Add", color = purple, fontWeight = FontWeight.Bold) }
            },
            dismissButton = {
                TextButton(onClick = { showAddConsignor = false }) { Text("Cancel", color = Color(0xFF555555)) }
            }
        )
    }

    // ── Add Card dialog ─────────────────────────────────────────────────────────
    showAddItemFor?.let { consignor ->
        var cardName by remember { mutableStateOf("") }
        var setName by remember { mutableStateOf("") }
        var condition by remember { mutableStateOf("NM") }
        var askingPrice by remember { mutableStateOf("") }
        var payoutOverride by remember { mutableStateOf("") }
        val conditions = listOf("NM", "LP", "MP", "HP", "DMG")
        val defaultPayout = remember(askingPrice, consignor.payoutRate) {
            val p = askingPrice.toDoubleOrNull() ?: 0.0
            p * consignor.payoutRate
        }

        AlertDialog(
            onDismissRequest = { showAddItemFor = null },
            containerColor = Color(0xFF1A1A1A),
            shape = RoundedCornerShape(16.dp),
            title = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.Style, null, tint = purple, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("Add Card — ${consignor.name}", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                }
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(value = cardName, onValueChange = { cardName = it },
                        label = { Text("Card Name *", color = Color(0xFF888888)) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White),
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = setName, onValueChange = { setName = it },
                        label = { Text("Set Name (optional)", color = Color(0xFF888888)) },
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White),
                        modifier = Modifier.fillMaxWidth())
                    Text("Condition", color = Color(0xFF888888), fontSize = 11.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        conditions.forEach { c ->
                            val sel = condition == c
                            OutlinedButton(
                                onClick = { condition = c },
                                modifier = Modifier.height(32.dp),
                                colors = ButtonDefaults.outlinedButtonColors(containerColor = if (sel) purple.copy(alpha = 0.2f) else Color.Transparent, contentColor = if (sel) purple else Color(0xFF666666)),
                                border = BorderStroke(1.dp, if (sel) purple else Color(0xFF333333)),
                                contentPadding = PaddingValues(horizontal = 8.dp),
                                shape = RoundedCornerShape(6.dp)
                            ) { Text(c, fontSize = 11.sp, fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal) }
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(value = askingPrice, onValueChange = { askingPrice = it },
                            label = { Text("Asking $", color = Color(0xFF888888)) },
                            singleLine = true,
                            modifier = Modifier.weight(1f),
                            colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White))
                        OutlinedTextField(value = payoutOverride, onValueChange = { payoutOverride = it },
                            label = { Text("Payout $ (auto)", color = Color(0xFF888888)) },
                            placeholder = { Text("$${String.format("%.2f", defaultPayout)}", color = Color(0xFF444444)) },
                            singleLine = true,
                            modifier = Modifier.weight(1f),
                            colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = purple, unfocusedBorderColor = Color(0xFF333333), focusedTextColor = Color.White, unfocusedTextColor = Color.White))
                    }
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        if (cardName.isNotBlank()) {
                            val price = askingPrice.toDoubleOrNull() ?: 0.0
                            val payout = payoutOverride.toDoubleOrNull() ?: (price * consignor.payoutRate)
                            onIntent(POSIntent.AddConsignmentItem(
                                consignorId = consignor.id,
                                consignorName = consignor.name,
                                cardName = cardName.trim(),
                                setName = setName.trim(),
                                condition = condition,
                                askingPrice = price,
                                payoutAmount = payout
                            ))
                            showAddItemFor = null
                        }
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2D0A3A))
                ) { Text("Add Card", color = purple, fontWeight = FontWeight.Bold) }
            },
            dismissButton = {
                TextButton(onClick = { showAddItemFor = null }) { Text("Cancel", color = Color(0xFF555555)) }
            }
        )
    }
}

// ── CUSTOMER MANAGEMENT PANEL ────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CustomerManagementPanel(
    customers: List<CustomerEntity>,
    onAddCredit: (Int, Double) -> Unit,
    onCreateCustomer: (String, Double) -> Unit,
    onDeleteCustomer: (Int) -> Unit,
    onDismiss: () -> Unit,
    onCheckout: () -> Unit = {}
) {
    var showNewCustomerDialog by remember { mutableStateOf(false) }
    var creditTarget by remember { mutableStateOf<CustomerEntity?>(null) }
    var searchQuery by remember { mutableStateOf("") }
    val filtered = remember(searchQuery, customers) {
        if (searchQuery.isEmpty()) customers
        else customers.filter { it.name.contains(searchQuery, ignoreCase = true) }
    }

    if (showNewCustomerDialog) {
        NewCustomerDialog(
            onConfirm = { name, credit -> onCreateCustomer(name, credit); showNewCustomerDialog = false },
            onDismiss = { showNewCustomerDialog = false }
        )
    }
    if (creditTarget != null) {
        AddCreditDialog(
            customer = creditTarget!!,
            onConfirm = { amount -> onAddCredit(creditTarget!!.id, amount); creditTarget = null },
            onDismiss = { creditTarget = null }
        )
    }

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.85f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(580.dp).heightIn(max = 780.dp).clickable(onClick = {}),
            shape = RoundedCornerShape(20.dp),
            color = Color(0xFF0D0015)
        ) {
            Column(Modifier.fillMaxSize()) {
                // Header
                Row(
                    Modifier.fillMaxWidth().background(Color(0xFF1A0D2A)).padding(horizontal = 20.dp, vertical = 16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.People, null, tint = Color(0xFFCE93D8), modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(10.dp))
                        Text("STORE CREDITS", fontWeight = FontWeight.Black, color = Color(0xFFCE93D8), fontSize = 14.sp, letterSpacing = 2.sp)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Button(
                            onClick = { showNewCustomerDialog = true },
                            shape = RoundedCornerShape(10.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A1B9A)),
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                            modifier = Modifier.height(36.dp)
                        ) {
                            Icon(Icons.Default.PersonAdd, null, tint = Color.White, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("New Customer", fontWeight = FontWeight.Bold, fontSize = 12.sp)
                        }
                        Spacer(Modifier.width(8.dp))
                        IconButton(onClick = onDismiss) {
                            Icon(Icons.Default.Close, null, tint = Color.Gray)
                        }
                    }
                }
                HorizontalDivider(color = Color(0xFF3D1A5A))

                // Search
                OutlinedTextField(
                    value = searchQuery,
                    onValueChange = { searchQuery = it },
                    placeholder = { Text("Search customers…", color = Color(0xFF555555), fontSize = 13.sp) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                    leadingIcon = { Icon(Icons.Default.Search, null, tint = Color(0xFF9C5BB5), modifier = Modifier.size(18.dp)) },
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedBorderColor = Color(0xFFCE93D8),
                        unfocusedBorderColor = Color(0xFF3D1A5A),
                    )
                )

                if (filtered.isEmpty()) {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Icon(Icons.Default.People, null, tint = Color(0xFF2A1040), modifier = Modifier.size(48.dp))
                            Spacer(Modifier.height(8.dp))
                            Text(if (customers.isEmpty()) "No customers yet — tap New Customer to add one" else "No matches", color = Color(0xFF555555), fontSize = 13.sp)
                        }
                    }
                } else {
                    LazyColumn(
                        Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        items(filtered, key = { it.id }) { cust ->
                            var showDelete by remember { mutableStateOf(false) }
                            Surface(color = Color(0xFF1A0D2A), shape = RoundedCornerShape(12.dp)) {
                                Row(
                                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.weight(1f)) {
                                        Box(
                                            Modifier.size(36.dp).clip(CircleShape).background(Color(0xFF3D1A5A)),
                                            contentAlignment = Alignment.Center
                                        ) {
                                            Text(cust.name.take(1).uppercase(), color = Color(0xFFCE93D8), fontWeight = FontWeight.Black, fontSize = 15.sp)
                                        }
                                        Spacer(Modifier.width(12.dp))
                                        Column {
                                            Text(cust.name, color = Color.White, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                                            Text("Credit: $${String.format("%.2f", cust.storeCredit)}", color = Color(0xFFCE93D8), fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                                        if (cust.storeCredit > 0.0) {
                                            Button(
                                                onClick = { onDismiss(); onCheckout() },
                                                shape = RoundedCornerShape(8.dp),
                                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF4ADE80)),
                                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 6.dp),
                                                modifier = Modifier.height(32.dp)
                                            ) {
                                                Icon(Icons.Default.ShoppingCart, null, tint = Color.Black, modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(4.dp))
                                                Text("Checkout", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color.Black)
                                            }
                                        }
                                        Button(
                                            onClick = { creditTarget = cust },
                                            shape = RoundedCornerShape(8.dp),
                                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF4A0E7A)),
                                            contentPadding = PaddingValues(horizontal = 10.dp, vertical = 6.dp),
                                            modifier = Modifier.height(32.dp)
                                        ) {
                                            Icon(Icons.Default.Add, null, tint = Color.White, modifier = Modifier.size(14.dp))
                                            Spacer(Modifier.width(4.dp))
                                            Text("Add Credit", fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                        }
                                        if (!showDelete) {
                                            IconButton(onClick = { showDelete = true }, modifier = Modifier.size(32.dp)) {
                                                Icon(Icons.Default.DeleteOutline, null, tint = Color(0xFF444444), modifier = Modifier.size(18.dp))
                                            }
                                        } else {
                                            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                                IconButton(onClick = { showDelete = false }, modifier = Modifier.size(32.dp)) {
                                                    Icon(Icons.Default.Close, null, tint = Color.Gray, modifier = Modifier.size(16.dp))
                                                }
                                                IconButton(onClick = { onDeleteCustomer(cust.id) }, modifier = Modifier.size(32.dp)) {
                                                    Icon(Icons.Default.Delete, null, tint = Color(0xFFEF5350), modifier = Modifier.size(18.dp))
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun NewCustomerDialog(onConfirm: (String, Double) -> Unit, onDismiss: () -> Unit) {
    var name by remember { mutableStateOf("") }
    var creditEntry by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(0xFF1A0D2A),
        titleContentColor = Color(0xFFCE93D8),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.PersonAdd, null, tint = Color(0xFFCE93D8), modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text("New Customer", fontWeight = FontWeight.Black)
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("Customer Name *") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White, unfocusedTextColor = Color.White,
                        focusedBorderColor = Color(0xFFCE93D8), unfocusedBorderColor = Color(0xFF3D1A5A),
                        focusedLabelColor = Color(0xFFCE93D8), unfocusedLabelColor = Color(0xFF666666)
                    )
                )
                OutlinedTextField(
                    value = creditEntry,
                    onValueChange = { v -> if (v.toDoubleOrNull() != null || v.isEmpty()) creditEntry = v },
                    label = { Text("Starting Credit ($0.00 if none)") },
                    singleLine = true,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White, unfocusedTextColor = Color.White,
                        focusedBorderColor = Color(0xFFCE93D8), unfocusedBorderColor = Color(0xFF3D1A5A),
                        focusedLabelColor = Color(0xFFCE93D8), unfocusedLabelColor = Color(0xFF666666)
                    )
                )
            }
        },
        confirmButton = {
            Button(
                onClick = { if (name.isNotBlank()) onConfirm(name, creditEntry.toDoubleOrNull() ?: 0.0) },
                enabled = name.isNotBlank(),
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A1B9A), disabledContainerColor = Color(0xFF2A1040))
            ) { Text("CREATE", fontWeight = FontWeight.Black) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("CANCEL", color = Color.Gray) } }
    )
}

@Composable
fun AddCreditDialog(customer: CustomerEntity, onConfirm: (Double) -> Unit, onDismiss: () -> Unit) {
    var entry by remember { mutableStateOf("") }
    val amount = entry.toDoubleOrNull() ?: 0.0
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Color(0xFF1A0D2A),
        titleContentColor = Color(0xFFCE93D8),
        title = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(28.dp).clip(CircleShape).background(Color(0xFF3D1A5A)), contentAlignment = Alignment.Center) {
                    Text(customer.name.take(1).uppercase(), color = Color(0xFFCE93D8), fontWeight = FontWeight.Black)
                }
                Spacer(Modifier.width(8.dp))
                Text("Add Credit — ${customer.name}", fontWeight = FontWeight.Black, fontSize = 14.sp)
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Current balance: $${String.format("%.2f", customer.storeCredit)}", color = Color(0xFFCE93D8), fontSize = 13.sp)
                OutlinedTextField(
                    value = entry,
                    onValueChange = { v ->
                        if (v.toDoubleOrNull() != null || v.isEmpty() || (v.endsWith(".") && v.count { it == '.' } == 1)) entry = v
                    },
                    label = { Text("Credit Amount") },
                    singleLine = true,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White, unfocusedTextColor = Color.White,
                        focusedBorderColor = Color(0xFFCE93D8), unfocusedBorderColor = Color(0xFF3D1A5A),
                        focusedLabelColor = Color(0xFFCE93D8), unfocusedLabelColor = Color(0xFF666666)
                    )
                )
                if (amount > 0) {
                    Text("New balance will be: $${String.format("%.2f", customer.storeCredit + amount)}", color = Color(0xFF4CAF50), fontSize = 12.sp)
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { if (amount > 0) onConfirm(amount) },
                enabled = amount > 0,
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6A1B9A), disabledContainerColor = Color(0xFF2A1040))
            ) { Text("ADD  +$${String.format("%.2f", amount)}", fontWeight = FontWeight.Black) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("CANCEL", color = Color.Gray) } }
    )
}

@Composable
fun BulkScanDialog(
    state: POSViewState,
    onDismiss: () -> Unit,
    onCheckout: () -> Unit,
    onScanItem: (String) -> Unit,
    onRemoveItem: (CartItemEntity) -> Unit,
    identifyCardFromImage: suspend (String) -> String?,
    onConditionCycle: (CartItemEntity) -> Unit = {}
) {
    var showInnerScanner by remember { mutableStateOf(false) }
    var identifyResult by remember { mutableStateOf<String?>(null) }
    var isIdentifying by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    val cameraLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicturePreview()
    ) { bitmap ->
        if (bitmap != null) {
            scope.launch {
                isIdentifying = true
                identifyResult = null
                try {
                    val baos = java.io.ByteArrayOutputStream()
                    bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 85, baos)
                    val b64 = android.util.Base64.encodeToString(baos.toByteArray(), android.util.Base64.NO_WRAP)
                    identifyResult = identifyCardFromImage(b64)
                } catch (_: Exception) {
                    identifyResult = "Could not identify — try again"
                } finally {
                    isIdentifying = false
                }
            }
        }
    }

    androidx.compose.ui.window.Dialog(
        onDismissRequest = onDismiss,
        properties = androidx.compose.ui.window.DialogProperties(
            usePlatformDefaultWidth = false,
            dismissOnClickOutside = false
        )
    ) {
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0xFF0D0D0D))
        ) {
            Column(Modifier.fillMaxSize()) {
                // Header bar
                Row(
                    Modifier
                        .fillMaxWidth()
                        .background(VaultGrey)
                        .padding(horizontal = 16.dp, vertical = 12.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(Icons.Default.QrCodeScanner, null, tint = Gold, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("BULK SCAN MODE", color = Gold, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 2.sp)
                    Spacer(Modifier.weight(1f))
                    val itemCount = state.cartItems.sumOf { it.quantity }
                    if (itemCount > 0) {
                        Text("$itemCount item${if (itemCount != 1) "s" else ""}", color = Color.Gray, fontSize = 12.sp)
                        Spacer(Modifier.width(16.dp))
                    }
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Default.Close, null, tint = Color.Gray)
                    }
                }

                // Scanned items list
                LazyColumn(
                    Modifier
                        .weight(1f)
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    items(state.cartItems) { item ->
                        val cond = state.cardConditions[item.id] ?: "NM"
                        val condColor = when (cond) {
                            "NM"  -> Color(0xFF4CAF50)
                            "LP"  -> Color(0xFFCDDC39)
                            "MP"  -> Color(0xFFFF9800)
                            "HP"  -> Color(0xFFF44336)
                            "DMG" -> Color(0xFF8D2B1A)
                            else  -> Color(0xFF4CAF50)
                        }
                        Row(
                            Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(8.dp))
                                .background(VaultSurface)
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(
                                    item.name,
                                    color = Color.White,
                                    fontSize = 13.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                                Text(
                                    "x${item.quantity}  •  $${String.format("%.2f", item.price)} each",
                                    color = Color.Gray,
                                    fontSize = 11.sp
                                )
                            }
                            Box(
                                Modifier
                                    .clip(RoundedCornerShape(4.dp))
                                    .background(condColor.copy(alpha = 0.18f))
                                    .clickable { onConditionCycle(item) }
                                    .padding(horizontal = 6.dp, vertical = 3.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(cond, color = condColor, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                            }
                            Spacer(Modifier.width(6.dp))
                            Text(
                                "$${String.format("%.2f", item.price * item.quantity)}",
                                color = Gold,
                                fontWeight = FontWeight.Bold,
                                fontSize = 14.sp
                            )
                            Spacer(Modifier.width(4.dp))
                            IconButton(
                                onClick = { onRemoveItem(item) },
                                modifier = Modifier.size(32.dp)
                            ) {
                                Icon(
                                    Icons.Default.RemoveCircle,
                                    null,
                                    tint = Color(0xFFF87171),
                                    modifier = Modifier.size(18.dp)
                                )
                            }
                        }
                    }

                    if (state.cartItems.isEmpty()) {
                        item {
                            Box(
                                Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 48.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Icon(
                                        Icons.Default.QrCodeScanner,
                                        null,
                                        tint = Color(0xFF333333),
                                        modifier = Modifier.size(48.dp)
                                    )
                                    Spacer(Modifier.height(12.dp))
                                    Text(
                                        "No items yet",
                                        color = Color(0xFF555555),
                                        fontWeight = FontWeight.Bold,
                                        fontSize = 14.sp
                                    )
                                    Text(
                                        "Tap SCAN to add cards",
                                        color = Color(0xFF444444),
                                        fontSize = 12.sp
                                    )
                                }
                            }
                        }
                    }
                }

                // AI identify result banner
                if (isIdentifying) {
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF1A1A2E))
                            .padding(horizontal = 16.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            color = Color(0xFF6366F1),
                            strokeWidth = 2.dp
                        )
                        Spacer(Modifier.width(10.dp))
                        Text("Identifying card via AI...", color = Color(0xFF818CF8), fontSize = 12.sp)
                    }
                }
                identifyResult?.let { result ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF0D1F0D))
                            .padding(horizontal = 16.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(Icons.Default.AutoAwesome, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(8.dp))
                        Text(
                            result,
                            color = Color(0xFF4ADE80),
                            fontSize = 12.sp,
                            modifier = Modifier.weight(1f)
                        )
                        TextButton(onClick = { identifyResult = null }) {
                            Text("OK", color = Color.Gray, fontSize = 10.sp)
                        }
                    }
                }

                // Total bar
                Row(
                    Modifier
                        .fillMaxWidth()
                        .background(Color(0xFF111111))
                        .padding(horizontal = 20.dp, vertical = 14.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text("CART TOTAL", color = Color(0xFF555555), fontSize = 10.sp, letterSpacing = 1.5.sp)
                        Text(
                            "${state.cartItems.sumOf { it.quantity }} items",
                            color = Color.Gray,
                            fontSize = 11.sp
                        )
                    }
                    Text(
                        "$${String.format("%.2f", state.cartItems.sumOf { it.price * it.quantity })}",
                        color = Color.White,
                        fontWeight = FontWeight.Black,
                        fontSize = 26.sp
                    )
                }

                // Action buttons
                Row(
                    Modifier
                        .fillMaxWidth()
                        .background(VaultGrey)
                        .padding(12.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    OutlinedButton(
                        onClick = { cameraLauncher.launch(null) },
                        modifier = Modifier.weight(1f),
                        border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF6366F1)),
                        shape = RoundedCornerShape(10.dp),
                        contentPadding = PaddingValues(vertical = 14.dp)
                    ) {
                        Icon(Icons.Default.CameraAlt, null, tint = Color(0xFF6366F1), modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("ID CARD", color = Color(0xFF6366F1), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                    OutlinedButton(
                        onClick = { showInnerScanner = true },
                        modifier = Modifier.weight(1f),
                        border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF444444)),
                        shape = RoundedCornerShape(10.dp),
                        contentPadding = PaddingValues(vertical = 14.dp)
                    ) {
                        Icon(Icons.Default.QrCodeScanner, null, tint = Color.White, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("SCAN", color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                    Button(
                        onClick = onCheckout,
                        enabled = state.cartItems.isNotEmpty(),
                        modifier = Modifier.weight(2f),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Gold,
                            disabledContainerColor = Color(0xFF3A3A3A)
                        ),
                        shape = RoundedCornerShape(10.dp),
                        contentPadding = PaddingValues(vertical = 14.dp)
                    ) {
                        Icon(Icons.Default.Payment, null, tint = VaultBlack, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "CHECKOUT NOW",
                            color = if (state.cartItems.isNotEmpty()) VaultBlack else Color.Gray,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Black
                        )
                    }
                }
            }

            if (showInnerScanner) {
                CompactQrScannerDialog(
                    prompt = "Scan card barcode or QR code",
                    onDismiss = { showInnerScanner = false },
                    onResult = { code ->
                        onScanItem(code)
                        showInnerScanner = false
                    }
                )
            }
        }
    }
}

fun sendEodEmail(
    context: android.content.Context,
    sales: List<SaleEntity>,
    openingFloat: Double = 0.0,
    closingCount: Double = 0.0
) {
    val dateStr = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(java.util.Date())
    val sb = StringBuilder()
    sb.appendLine("HanryxVault POS — End of Day Report")
    sb.appendLine("Date: $dateStr")
    sb.appendLine("Generated: ${java.text.SimpleDateFormat("h:mm a", java.util.Locale.US).format(java.util.Date())}")
    sb.appendLine()

    val activeSales = sales.filter { !it.isRefunded }
    val totalRevenue = activeSales.sumOf { it.totalAmount }
    val totalSubtotal = activeSales.sumOf { it.subtotal }
    val totalTax = activeSales.sumOf { it.taxAmount }
    val totalTip = activeSales.sumOf { it.tipAmount }
    val refundCount = sales.count { it.isRefunded }
    val byMethod = activeSales.groupBy { it.paymentMethod.uppercase() }
    val byMethodRevenue = byMethod.mapValues { (_, v) -> v.sumOf { it.totalAmount } }
    val byMethodCount = byMethod.mapValues { (_, v) -> v.size }

    sb.appendLine("=== SUMMARY ===")
    sb.appendLine("Transactions: ${activeSales.size}  |  Refunds: $refundCount")
    sb.appendLine("Subtotal:     $${"%.2f".format(totalSubtotal)}")
    sb.appendLine("Tax:          $${"%.2f".format(totalTax)}")
    if (totalTip > 0) sb.appendLine("Tips:         $${"%.2f".format(totalTip)}")
    sb.appendLine("TOTAL:        $${"%.2f".format(totalRevenue)}")
    sb.appendLine()
    sb.appendLine("=== BY PAYMENT METHOD ===")
    byMethodRevenue.forEach { (method, amount) ->
        val count = byMethodCount[method] ?: 0
        sb.appendLine("$method: $${"%.2f".format(amount)} ($count tx)")
    }

    if (openingFloat > 0) {
        val cashRevenue = byMethodRevenue["CASH"] ?: 0.0
        val tradeCashOut = byMethodRevenue["TRADE_CASH"] ?: 0.0
        val expectedDrawer = openingFloat + cashRevenue - tradeCashOut
        sb.appendLine()
        sb.appendLine("=== CASH RECONCILIATION ===")
        sb.appendLine("Opening Float:   $${"%.2f".format(openingFloat)}")
        sb.appendLine("Cash Sales:      $${"%.2f".format(cashRevenue)}")
        if (tradeCashOut > 0) {
            sb.appendLine("Trade Payouts:  -$${"%.2f".format(tradeCashOut)}")
        }
        sb.appendLine("Expected Drawer: $${"%.2f".format(expectedDrawer)}")
        if (closingCount > 0) {
            val variance = closingCount - expectedDrawer
            sb.appendLine("Counted:         $${"%.2f".format(closingCount)}")
            val sign = if (variance >= 0) "+" else ""
            sb.appendLine("Variance:        $sign${"%.2f".format(variance)}")
        }
    }

    sb.appendLine()
    sb.appendLine("=== TRANSACTION LOG ===")
    sb.appendLine("ID,Time,Method,Total,Tip,Refunded")
    val fmt = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US)
    sales.forEach { s ->
        sb.appendLine("${s.id},${fmt.format(java.util.Date(s.timestamp))},${s.paymentMethod},${"%.2f".format(s.totalAmount)},${"%.2f".format(s.tipAmount)},${s.isRefunded}")
    }

    val intent = android.content.Intent(android.content.Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(android.content.Intent.EXTRA_EMAIL, arrayOf("noah.gansen@hanryxvault.company"))
        putExtra(android.content.Intent.EXTRA_SUBJECT, "HanryxVault POS — End of Day Report — $dateStr")
        putExtra(android.content.Intent.EXTRA_TEXT, sb.toString())
    }
    context.startActivity(android.content.Intent.createChooser(intent, "Send EOD Report").apply {
        addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
    })
}

// ─────────────────────────────────────────────────────────────────────────────────────
//  REPRICING QUEUE SHEET
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RepricingQueueSheet(
    queue: List<RepriceSuggestion>,
    onDismiss: () -> Unit,
    onApply: (qrCode: String, newPrice: Double) -> Unit,
    onDismissSuggestion: (qrCode: String) -> Unit,
    onApplyAll: () -> Unit
) {
    val amber = Color(0xFFF59E0B)
    val green = Color(0xFF4ADE80)
    val red = Color(0xFFFF6B6B)
    val bg = Color(0xFF131313)
    val surf = Color(0xFF1A1A1A)
    val border = Color(0xFF2A2A2A)

    var selectedTab by remember { mutableStateOf(0) }
    val overpriced = queue.filter { it.pctChange < 0 }.sortedBy { it.pctChange }
    val underpriced = queue.filter { it.pctChange > 0 }.sortedByDescending { it.pctChange }
    val displayed = if (selectedTab == 0) overpriced else underpriced
    val totalOverImpact = overpriced.sumOf { it.product.price - it.suggestedPrice }
    val totalUnderGain = underpriced.sumOf { it.suggestedPrice - it.product.price }

    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = bg,
        dragHandle = null,
        windowInsets = WindowInsets(0)
    ) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(0.85f).navigationBarsPadding()) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(top = 16.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.TrendingUp, null, tint = amber, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("Repricing Queue", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    Spacer(Modifier.weight(1f))
                    IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                        Icon(Icons.Default.Close, null, tint = Color(0xFF666666), modifier = Modifier.size(16.dp))
                    }
                }
                Text(
                    "Market moved ≥20 % vs your listed price. Update to stay competitive.",
                    color = Color(0xFF666666), fontSize = 11.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 14.dp)
                )

                Row(Modifier.fillMaxWidth().background(Color(0xFF0D0D0D), RoundedCornerShape(10.dp)).padding(3.dp)) {
                    listOf(
                        Triple("Overpriced", overpriced.size, red),
                        Triple("Underpriced", underpriced.size, green)
                    ).forEachIndexed { idx, (label, count, tint) ->
                        val sel = selectedTab == idx
                        Surface(
                            onClick = { selectedTab = idx },
                            color = if (sel) surf else Color.Transparent,
                            shape = RoundedCornerShape(8.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Row(
                                Modifier.padding(vertical = 10.dp),
                                horizontalArrangement = Arrangement.Center,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(label, color = if (sel) tint else Color(0xFF555555), fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal, fontSize = 12.sp)
                                if (count > 0) {
                                    Spacer(Modifier.width(6.dp))
                                    Surface(color = tint.copy(alpha = if (sel) 0.2f else 0.1f), shape = CircleShape, modifier = Modifier.size(20.dp)) {
                                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                            Text("$count", color = if (sel) tint else Color(0xFF555555), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Spacer(Modifier.height(12.dp))
                if (selectedTab == 0 && overpriced.isNotEmpty()) {
                    Surface(Modifier.fillMaxWidth(), color = red.copy(alpha = 0.08f), shape = RoundedCornerShape(8.dp)) {
                        Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.TrendingDown, null, tint = red, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(8.dp))
                            Column {
                                Text("You're ${"$%,.2f".format(totalOverImpact)} above market", color = red, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                                Text("Lowering these may speed up sales", color = Color(0xFF888888), fontSize = 10.sp)
                            }
                        }
                    }
                } else if (selectedTab == 1 && underpriced.isNotEmpty()) {
                    Surface(Modifier.fillMaxWidth(), color = green.copy(alpha = 0.08f), shape = RoundedCornerShape(8.dp)) {
                        Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.AttachMoney, null, tint = green, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(8.dp))
                            Column {
                                Text("${"$%,.2f".format(totalUnderGain)} left on the table", color = green, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                                Text("Raise prices to capture market value", color = Color(0xFF888888), fontSize = 10.sp)
                            }
                        }
                    }
                }
                Spacer(Modifier.height(8.dp))
            }
            HorizontalDivider(color = border)

            if (displayed.isEmpty()) {
                Box(Modifier.fillMaxWidth().padding(vertical = 40.dp), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Default.CheckCircle, null, tint = Color(0xFF333333), modifier = Modifier.size(40.dp))
                        Spacer(Modifier.height(8.dp))
                        Text(
                            if (selectedTab == 0) "No overpriced cards — your prices are competitive!" else "No underpriced cards — you're capturing full value!",
                            color = Color(0xFF555555), fontSize = 13.sp, textAlign = TextAlign.Center
                        )
                    }
                }
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f).padding(horizontal = 20.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                    contentPadding = PaddingValues(vertical = 8.dp)
                ) {
                    items(displayed.size, key = { displayed[it].product.qrCode }) { index ->
                        val suggestion = displayed[index]
                        val pct = suggestion.pctChange
                        val isSpike = pct > 0
                        val pctColor = if (isSpike) green else red
                        val pctText = "${if (isSpike) "+" else ""}${"%.0f".format(pct)}%"
                        val gap = kotlin.math.abs(suggestion.suggestedPrice - suggestion.product.price)
                        val cond = suggestion.condition
                        val condColor = when (cond) {
                            "NM" -> green; "LP" -> amber; "MP" -> Color(0xFFFF8C42); "HP" -> red; "DMG" -> Color(0xFFAA3333); else -> Color(0xFF888888)
                        }
                        val itemVisible = remember { Animatable(0f) }
                        LaunchedEffect(Unit) { itemVisible.animateTo(1f, tween(300, delayMillis = index * 30)) }
                        Surface(
                            Modifier.fillMaxWidth()
                                .graphicsLayer(alpha = itemVisible.value, translationX = (1f - itemVisible.value) * 40f),
                            color = surf, shape = RoundedCornerShape(10.dp)
                        ) {
                            Column(Modifier.padding(12.dp)) {
                                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                    Column(Modifier.weight(1f)) {
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Text(suggestion.product.name, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
                                            Spacer(Modifier.width(6.dp))
                                            Surface(color = condColor.copy(alpha = 0.18f), shape = RoundedCornerShape(3.dp)) {
                                                Text(cond, color = condColor, fontSize = 9.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                            }
                                        }
                                        Spacer(Modifier.height(4.dp))
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Text("Your: ", color = Color(0xFF666666), fontSize = 11.sp)
                                            Text("$${"%,.2f".format(suggestion.product.price)}", color = if (isSpike) Color(0xFF888888) else red, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                            Icon(Icons.Default.ArrowForward, null, tint = Color(0xFF444444), modifier = Modifier.size(14.dp).padding(horizontal = 2.dp))
                                            Text("Mkt: ", color = Color(0xFF666666), fontSize = 11.sp)
                                            Text("$${"%,.2f".format(suggestion.suggestedPrice)}", color = if (isSpike) green else Color(0xFF888888), fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                    Spacer(Modifier.width(10.dp))
                                    Column(horizontalAlignment = Alignment.End) {
                                        Surface(color = pctColor.copy(alpha = 0.12f), shape = RoundedCornerShape(4.dp)) {
                                            Text(pctText, color = pctColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 6.dp, vertical = 3.dp))
                                        }
                                        Spacer(Modifier.height(2.dp))
                                        Text("${if (isSpike) "+" else "-"}$${"%,.2f".format(gap)}", color = pctColor.copy(alpha = 0.7f), fontSize = 10.sp)
                                    }
                                }
                                Spacer(Modifier.height(8.dp))
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End, verticalAlignment = Alignment.CenterVertically) {
                                    TextButton(onClick = { onDismissSuggestion(suggestion.product.qrCode) }, contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp)) {
                                        Text("Skip", color = Color(0xFF555555), fontSize = 11.sp)
                                    }
                                    Spacer(Modifier.width(8.dp))
                                    Button(
                                        onClick = { onApply(suggestion.product.qrCode, suggestion.suggestedPrice) },
                                        colors = ButtonDefaults.buttonColors(containerColor = if (isSpike) green else amber),
                                        contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp),
                                        modifier = Modifier.height(34.dp),
                                        shape = RoundedCornerShape(8.dp)
                                    ) {
                                        Icon(if (isSpike) Icons.Default.TrendingUp else Icons.Default.TrendingDown, null, tint = Color.Black, modifier = Modifier.size(14.dp))
                                        Spacer(Modifier.width(4.dp))
                                        Text(if (isSpike) "Raise" else "Lower", color = Color.Black, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                    }
                                }
                            }
                        }
                    }
                }

                Column(Modifier.fillMaxWidth().background(bg).padding(horizontal = 20.dp).padding(bottom = 16.dp, top = 8.dp)) {
                    Button(
                        onClick = { onApplyAll(); onDismiss() },
                        modifier = Modifier.fillMaxWidth().height(48.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = amber),
                        shape = RoundedCornerShape(10.dp)
                    ) {
                        Icon(Icons.Default.DoneAll, null, tint = Color.Black, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Update All ${queue.size} Cards", color = Color.Black, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────────────
//  TABLET INCOMING OFFER SHEET  (admin-pushed trade-in offer)
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TabletIncomingOfferSheet(
    offer: TabletOfferResponse,
    offerStatus: String,
    onAcceptAndSign: (modifiedItems: List<TabletOfferItem>, modifiedCash: Double, modifiedCredit: Double) -> Unit,
    onDecline: () -> Unit,
    onDismiss: () -> Unit,
    onModify: (List<TabletOfferItem>, Double, Double) -> Unit = { _, _, _ -> }
) {
    val bg = Color(0xFF1A1A2E)
    val surface = Color(0xFF16213E)
    val green = Color(0xFF4ADE80)
    val amber = Color(0xFFFBBF24)
    val red = Color(0xFFEF4444)
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    // Local editable copy of the offer items. Customer can delete a card
    // or reduce its cash offer before signing. Totals are recomputed from
    // localItems and the credit/cash ratio is preserved from the original offer.
    var localItems by remember(offer.ti_id) { mutableStateOf(offer.items) }
    var editingItem by remember { mutableStateOf<TabletOfferItem?>(null) }
    var editPriceText by remember { mutableStateOf("") }

    val ratio = if (offer.total_cash > 0.0) offer.total_credit / offer.total_cash else 1.20
    val localCash = localItems.sumOf { it.offer }
    val localCredit = localCash * ratio
    val isModified = localItems != offer.items

    // Push modifications back to the server (best-effort, non-blocking).
    LaunchedEffect(localItems) {
        if (isModified) onModify(localItems, localCash, localCredit)
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = bg,
        dragHandle = null,
        shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight(0.92f)
                .navigationBarsPadding()
                .padding(horizontal = 20.dp)
                .padding(top = 20.dp)
        ) {
            // Header
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text(
                        "Trade-In Offer",
                        color = Color.White,
                        fontSize = 20.sp,
                        fontWeight = FontWeight.Bold
                    )
                    if (offer.customer.isNotEmpty()) {
                        Text(
                            offer.customer,
                            color = Color.White.copy(alpha = 0.6f),
                            fontSize = 13.sp
                        )
                    }
                    if (offer.reference.isNotEmpty()) {
                        Text(
                            "Ref: ${offer.reference}",
                            color = amber.copy(alpha = 0.8f),
                            fontSize = 11.sp
                        )
                    }
                }
                Column(horizontalAlignment = Alignment.End) {
                    Surface(
                        color = green.copy(alpha = 0.15f),
                        shape = RoundedCornerShape(20.dp)
                    ) {
                        Text(
                            "${localItems.size} card${if (localItems.size != 1) "s" else ""}",
                            color = green,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)
                        )
                    }
                    if (isModified) {
                        Spacer(Modifier.height(4.dp))
                        Text("MODIFIED", color = amber, fontSize = 9.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
                    }
                }
            }

            Spacer(Modifier.height(16.dp))
            Divider(color = Color.White.copy(alpha = 0.08f))
            Spacer(Modifier.height(12.dp))

            // Scrollable middle: items list + totals. Action buttons stay pinned below.
            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState())
            ) {
            // Items list (uses localItems so deletions/edits show immediately)
            if (localItems.isNotEmpty()) {
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    localItems.forEach { item ->
                        Surface(
                            color = surface,
                            shape = RoundedCornerShape(10.dp)
                        ) {
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(horizontal = 14.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(
                                        item.name,
                                        color = Color.White,
                                        fontSize = 13.sp,
                                        fontWeight = FontWeight.Medium,
                                        maxLines = 2
                                    )
                                    if (item.condition.isNotEmpty()) {
                                        Text(
                                            item.condition,
                                            color = amber.copy(alpha = 0.7f),
                                            fontSize = 11.sp
                                        )
                                    }
                                    if (item.market > 0) {
                                        Text(
                                            "Market: $${String.format("%.2f", item.market)}",
                                            color = Color.White.copy(alpha = 0.4f),
                                            fontSize = 11.sp
                                        )
                                    }
                                }
                                Spacer(Modifier.width(8.dp))
                                // Tap price to edit (lower) the cash offer
                                Surface(
                                    color = green.copy(alpha = 0.10f),
                                    shape = RoundedCornerShape(8.dp),
                                    onClick = {
                                        editingItem = item
                                        editPriceText = String.format("%.2f", item.offer)
                                    }
                                ) {
                                    Column(
                                        horizontalAlignment = Alignment.End,
                                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
                                    ) {
                                        Text(
                                            "$${String.format("%.2f", item.offer)}",
                                            color = green,
                                            fontSize = 15.sp,
                                            fontWeight = FontWeight.Bold
                                        )
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Icon(Icons.Default.Edit, null, tint = green.copy(alpha = 0.6f), modifier = Modifier.size(9.dp))
                                            Spacer(Modifier.width(2.dp))
                                            Text(
                                                "tap to edit",
                                                color = green.copy(alpha = 0.6f),
                                                fontSize = 9.sp
                                            )
                                        }
                                    }
                                }
                                Spacer(Modifier.width(6.dp))
                                // Remove this card from the offer
                                IconButton(
                                    onClick = { localItems = localItems.filter { it.id != item.id || it.qr_code != item.qr_code } },
                                    modifier = Modifier.size(36.dp)
                                ) {
                                    Icon(
                                        Icons.Default.Close,
                                        contentDescription = "Remove ${item.name}",
                                        tint = red.copy(alpha = 0.85f),
                                        modifier = Modifier.size(20.dp)
                                    )
                                }
                            }
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))
            } else {
                // All items removed — show a friendly placeholder
                Surface(
                    color = red.copy(alpha = 0.10f),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Info, null, tint = red, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(8.dp))
                        Text("No cards left in this offer. Decline to cancel.", color = Color.White.copy(alpha = 0.8f), fontSize = 12.sp)
                    }
                }
                Spacer(Modifier.height(12.dp))
            }

            // Totals row
            Surface(
                color = green.copy(alpha = 0.1f),
                shape = RoundedCornerShape(12.dp)
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 14.dp),
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Column {
                        Text("Cash Total", color = Color.White.copy(alpha = 0.6f), fontSize = 12.sp)
                        Text(
                            "$${String.format("%.2f", localCash)}",
                            color = green,
                            fontSize = 22.sp,
                            fontWeight = FontWeight.Bold
                        )
                        if (isModified && offer.total_cash > 0) {
                            Text(
                                "was $${String.format("%.2f", offer.total_cash)}",
                                color = Color.White.copy(alpha = 0.4f),
                                fontSize = 10.sp,
                                textDecoration = androidx.compose.ui.text.style.TextDecoration.LineThrough
                            )
                        }
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text("Store Credit", color = Color.White.copy(alpha = 0.6f), fontSize = 12.sp)
                        Text(
                            "$${String.format("%.2f", localCredit)}",
                            color = amber,
                            fontSize = 22.sp,
                            fontWeight = FontWeight.Bold
                        )
                        if (isModified && offer.total_credit > 0) {
                            Text(
                                "was $${String.format("%.2f", offer.total_credit)}",
                                color = Color.White.copy(alpha = 0.4f),
                                fontSize = 10.sp,
                                textDecoration = androidx.compose.ui.text.style.TextDecoration.LineThrough
                            )
                        }
                    }
                }
            }

            Spacer(Modifier.height(20.dp))
            } // end scrollable middle Column

            // ── Sticky action buttons (always visible above nav bar) ─────────────
            if (offerStatus == "accepted") {
                Surface(
                    color = green.copy(alpha = 0.15f),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier.padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.Center
                    ) {
                        Icon(Icons.Default.CheckCircle, null, tint = green, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(8.dp))
                        Text("Offer Accepted", color = green, fontWeight = FontWeight.Bold)
                    }
                }
            } else if (offerStatus == "rejected") {
                Surface(
                    color = red.copy(alpha = 0.15f),
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier.padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.Center
                    ) {
                        Icon(Icons.Default.Cancel, null, tint = red, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(8.dp))
                        Text("Offer Declined", color = red, fontWeight = FontWeight.Bold)
                    }
                }
            } else {
                // Accept & Sign — disabled if every card has been removed
                Button(
                    onClick = { onAcceptAndSign(localItems, localCash, localCredit) },
                    enabled = localItems.isNotEmpty(),
                    modifier = Modifier.fillMaxWidth().height(52.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = green,
                        disabledContainerColor = green.copy(alpha = 0.3f)
                    ),
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Icon(Icons.Default.Draw, null, tint = Color.Black, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Accept & Sign  •  $${String.format("%.2f", localCash)} cash",
                        color = Color.Black,
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    )
                }
                Spacer(Modifier.height(10.dp))
                OutlinedButton(
                    onClick = onDecline,
                    modifier = Modifier.fillMaxWidth().height(48.dp),
                    border = BorderStroke(1.dp, red.copy(alpha = 0.5f)),
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Icon(Icons.Default.Cancel, null, tint = red, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("Decline Offer", color = red, fontSize = 14.sp)
                }
            }
        }
    }

    // ── Price-edit dialog (lower the cash offer for one card) ───────────────
    val editing = editingItem
    if (editing != null) {
        AlertDialog(
            onDismissRequest = { editingItem = null },
            containerColor = Color(0xFF16213E),
            title = { Text("Adjust cash offer", color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.Bold) },
            text = {
                Column {
                    Text(editing.name, color = Color.White.copy(alpha = 0.85f), fontSize = 13.sp)
                    if (editing.market > 0) {
                        Text("Market: $${String.format("%.2f", editing.market)}", color = Color.White.copy(alpha = 0.5f), fontSize = 11.sp)
                    }
                    Text("Original offer: $${String.format("%.2f", editing.offer)}", color = Color.White.copy(alpha = 0.5f), fontSize = 11.sp)
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = editPriceText,
                        onValueChange = { new ->
                            // Allow only digits + a single decimal point
                            if (new.matches(Regex("^\\d*\\.?\\d{0,2}$"))) editPriceText = new
                        },
                        label = { Text("New cash offer", color = Color.White.copy(alpha = 0.6f)) },
                        leadingIcon = { Text("$", color = green, fontSize = 16.sp, fontWeight = FontWeight.Bold) },
                        singleLine = true,
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                            keyboardType = KeyboardType.Decimal,
                            imeAction = androidx.compose.ui.text.input.ImeAction.Done
                        ),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = green,
                            unfocusedBorderColor = Color.White.copy(alpha = 0.2f),
                            cursorColor = green
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val newPrice = editPriceText.toDoubleOrNull()
                        if (newPrice != null && newPrice >= 0.0) {
                            localItems = localItems.map {
                                if (it.id == editing.id && it.qr_code == editing.qr_code) it.copy(offer = newPrice) else it
                            }
                            editingItem = null
                        }
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = green)
                ) {
                    Text("Save", color = Color.Black, fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { editingItem = null }) {
                    Text("Cancel", color = Color.White.copy(alpha = 0.7f))
                }
            }
        )
    }
}

// ─────────────────────────────────────────────────────────────────────────────────────
//  TRADE-IN SHEET
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TradeInSheet(
    state: POSViewState,
    onDismiss: () -> Unit,
    onRemoveItem: (qrCode: String) -> Unit,
    onCancel: () -> Unit,
    onFinalize: (customerName: String, offerType: TradeOfferType) -> Unit,
    onApplyToCart: (customerName: String) -> Unit,
    onCheckout: () -> Unit = {},
    onSetManualCredit: (Float) -> Unit = {},
    onManualAdd: (name: String, marketPrice: Double) -> Unit = { _, _ -> },
    onSearchMarketPrice: (query: String) -> Unit = {},
    onAddToCart: (name: String, price: Double) -> Unit = { _, _ -> },
    onCashOut: (name: String, price: Double) -> Unit = { _, _ -> },
    onRequestSigning: (items: List<TradeInItem>, action: () -> Unit) -> Unit = { _, action -> action() },
    onUpdateOffer: (qrCode: String, newBuyOffer: Double, newTradeCredit: Double) -> Unit = { _, _, _ -> },
    onScanOutgoing: (qrCode: String) -> Unit = {},
    onRemoveOutgoing: (qrCode: String) -> Unit = {},
    onUpdateOutgoingPrice: (qrCode: String, newPrice: Double) -> Unit = { _, _ -> },
    onRestoreCanceled: (snapshotId: String) -> Unit = {},
    onDismissCanceledBanner: () -> Unit = {}
) {
    val amber  = Color(0xFFF59E0B)
    val green  = Color(0xFF4ADE80)
    val blue   = Color(0xFF60A5FA)
    val bg     = Color(0xFF131313)
    val surf   = Color(0xFF1A1A1A)
    val surf2  = Color(0xFF222222)
    val border = Color(0xFF2A2A2A)
    val muted  = Color(0xFF666666)
    val dim    = Color(0xFF444444)

    var phase by remember { mutableStateOf(0) }
    var customerName by remember { mutableStateOf("") }
    var selectedOffer by remember { mutableStateOf(TradeOfferType.CASH) }
    var manualSearchQuery by remember { mutableStateOf("") }
    var manualSearching by remember { mutableStateOf(false) }
    var editingItem by remember { mutableStateOf<TradeInItem?>(null) }
    var editBuyOffer by remember { mutableStateOf("") }
    var editTradeCredit by remember { mutableStateOf("") }
    var showManualResult by remember { mutableStateOf(false) }
    var showCardScanner by remember { mutableStateOf(false) }
    var quickPrice by remember { mutableStateOf("") }
    var showQuickEstimate by remember { mutableStateOf(false) }
    var manualCreditInput by remember { mutableStateOf(if (state.widgetTradeCredit > 0f) "%.2f".format(state.widgetTradeCredit) else "") }

    val widgetCreditD = state.widgetTradeCredit.toDouble()
    val outgoingTotal = state.tradeOutItems.sumOf { it.product.price }
    val cashTotalGross   = state.tradeInItems.sumOf { it.buyOffer }    + widgetCreditD * (8.0 / 9.0)
    val creditTotalGross = state.tradeInItems.sumOf { it.tradeCredit } + widgetCreditD
    val cashTotal   = (cashTotalGross   - outgoingTotal).coerceAtLeast(0.0)
    val creditTotal = (creditTotalGross - outgoingTotal).coerceAtLeast(0.0)
    val itemCount   = state.tradeInItems.size
    val hasItems    = itemCount > 0 || state.widgetTradeCredit > 0f
    var showOutgoingScanner by remember { mutableStateOf(false) }
    var showRecentCanceled by remember { mutableStateOf(false) }
    var editingOutgoing by remember { mutableStateOf<TradeInItem?>(null) }
    var editOutgoingPrice by remember { mutableStateOf("") }
    val buyPctInt   = (state.tradeBuyPct * 100).toInt()
    val creditPctInt = (state.tradeCreditPct * 100).toInt()
    val quickParsed = quickPrice.toFloatOrNull() ?: 0f
    val quickCash   = quickParsed * state.tradeBuyPct.toFloat()
    val quickCredit = quickParsed * state.tradeCreditPct.toFloat()

    // ── Edit Offer Dialog ───────────────────────────────────────────────────
    editingItem?.let { item ->
        AlertDialog(
            onDismissRequest = { editingItem = null },
            containerColor = Color(0xFF1A1A1A),
            title = {
                Text(
                    text = item.product.name,
                    color = Color.White,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 2,
                    overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                )
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    if (item.marketPrice > 0) {
                        Text(
                            "Market price: $${"%,.2f".format(item.marketPrice)}",
                            color = Color(0xFF888888),
                            fontSize = 12.sp
                        )
                    }
                    OutlinedTextField(
                        value = editBuyOffer,
                        onValueChange = { editBuyOffer = it.filter { c -> c.isDigit() || c == '.' } },
                        label = { Text("Cash offer ($)", color = Color(0xFF888888), fontSize = 12.sp) },
                        singleLine = true,
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                        colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = green,
                            unfocusedBorderColor = Color(0xFF333333),
                            focusedTextColor = green,
                            unfocusedTextColor = green
                        )
                    )
                    OutlinedTextField(
                        value = editTradeCredit,
                        onValueChange = { editTradeCredit = it.filter { c -> c.isDigit() || c == '.' } },
                        label = { Text("Store credit ($)", color = Color(0xFF888888), fontSize = 12.sp) },
                        singleLine = true,
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                        colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = blue,
                            unfocusedBorderColor = Color(0xFF333333),
                            focusedTextColor = blue,
                            unfocusedTextColor = blue
                        )
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val newBuy    = editBuyOffer.toDoubleOrNull()    ?: item.buyOffer
                    val newCredit = editTradeCredit.toDoubleOrNull() ?: item.tradeCredit
                    onUpdateOffer(item.product.qrCode, newBuy, newCredit)
                    editingItem = null
                }) {
                    Text("Save", color = green, fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { editingItem = null }) {
                    Text("Cancel", color = Color(0xFF888888))
                }
            }
        )
    }

    // ── Edit outgoing-item price dialog ──────────────────────────────────────
    editingOutgoing?.let { oi ->
        AlertDialog(
            onDismissRequest = { editingOutgoing = null },
            containerColor = Color(0xFF1A1A1A),
            title = {
                Column {
                    Text("Set price (customer pays this)", color = Color(0xFF888888), fontSize = 11.sp)
                    Text(
                        text = oi.product.name,
                        color = Color.White,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Bold,
                        maxLines = 2,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                    )
                }
            },
            text = {
                OutlinedTextField(
                    value = editOutgoingPrice,
                    onValueChange = { editOutgoingPrice = it.filter { c -> c.isDigit() || c == '.' } },
                    label = { Text("Price ($)", color = Color(0xFF888888), fontSize = 12.sp) },
                    singleLine = true,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                    colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = blue,
                        unfocusedBorderColor = Color(0xFF333333),
                        focusedTextColor = blue,
                        unfocusedTextColor = blue
                    )
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val newPrice = editOutgoingPrice.toDoubleOrNull() ?: oi.product.price
                    onUpdateOutgoingPrice(oi.product.qrCode, newPrice)
                    editingOutgoing = null
                }) {
                    Text("Save", color = blue, fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { editingOutgoing = null }) {
                    Text("Cancel", color = Color(0xFF888888))
                }
            }
        )
    }

    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = bg,
        dragHandle = {
            Box(Modifier.fillMaxWidth().padding(top = 10.dp), contentAlignment = Alignment.Center) {
                Box(Modifier.width(40.dp).height(4.dp).background(border, RoundedCornerShape(2.dp)))
            }
        },
        windowInsets = WindowInsets(0),
        sheetMaxWidth = 600.dp
    ) {
        // CRITICAL: pin the sheet's content to 92% of screen height so the inner
        // verticalScroll() actually has bounded space to scroll against. Without
        // this, the column grows past the screen edge and the REVIEW OFFER /
        // CASH OUT buttons get hidden under the system nav bar with no way to scroll.
        Column(
            Modifier.fillMaxWidth().fillMaxHeight(0.92f).navigationBarsPadding()
        ) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(top = 4.dp, bottom = 12.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Surface(color = amber.copy(alpha = 0.15f), shape = RoundedCornerShape(6.dp)) {
                    Row(Modifier.padding(horizontal = 10.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.SwapHoriz, null, tint = amber, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("TRADE-IN", color = amber, fontWeight = FontWeight.Black, fontSize = 12.sp, letterSpacing = 1.sp)
                    }
                }
                if (itemCount > 0) {
                    Spacer(Modifier.width(8.dp))
                    Surface(color = amber, shape = CircleShape, modifier = Modifier.size(22.dp)) {
                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text("$itemCount", color = Color.Black, fontSize = 11.sp, fontWeight = FontWeight.Black)
                        }
                    }
                }
                Spacer(Modifier.weight(1f))
                if (hasItems) {
                    Column(horizontalAlignment = Alignment.End) {
                        Text("$${"%,.2f".format(cashTotal)}", color = green, fontSize = 14.sp, fontWeight = FontWeight.Black)
                        Text("cash", color = muted, fontSize = 9.sp)
                    }
                    Spacer(Modifier.width(12.dp))
                    Column(horizontalAlignment = Alignment.End) {
                        Text("$${"%,.2f".format(creditTotal)}", color = blue, fontSize = 14.sp, fontWeight = FontWeight.Black)
                        Text("credit", color = muted, fontSize = 9.sp)
                    }
                }
            }

            // ── Undo banner (last canceled / rejected trade) ────────────────────
            state.canceledTradeBanner?.let { snap ->
                val itemsTotal = snap.incoming.size + snap.outgoing.size
                val reasonLabel = when (snap.reason) {
                    "tablet_rejected" -> "Customer rejected the tablet offer"
                    "cleared" -> "Trade canceled"
                    else -> "Trade canceled"
                }
                Surface(
                    color = Color(0xFF3A1F00),
                    border = BorderStroke(1.dp, amber),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(bottom = 8.dp)
                ) {
                    Row(
                        Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(Icons.Default.Restore, null, tint = amber, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(8.dp))
                        Column(Modifier.weight(1f)) {
                            Text(reasonLabel, color = amber, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                            Text(
                                "$itemsTotal item${if (itemsTotal == 1) "" else "s"}" +
                                    (if (snap.customerName.isNotBlank()) " • ${snap.customerName}" else ""),
                                color = Color(0xFFCCCCCC), fontSize = 10.sp
                            )
                        }
                        TextButton(onClick = { onRestoreCanceled(snap.id) }) {
                            Text("UNDO", color = amber, fontWeight = FontWeight.Black, fontSize = 12.sp)
                        }
                        IconButton(onClick = onDismissCanceledBanner, modifier = Modifier.size(28.dp)) {
                            Icon(Icons.Default.Close, null, tint = muted, modifier = Modifier.size(14.dp))
                        }
                    }
                }
            }

            if (phase == 0) {
                HorizontalDivider(color = border, thickness = 0.5.dp)
            }

            AnimatedContent(
                targetState = phase,
                transitionSpec = {
                    if (targetState > initialState) {
                        slideInHorizontally { it / 2 } + fadeIn() togetherWith slideOutHorizontally { -it / 2 } + fadeOut()
                    } else {
                        slideInHorizontally { -it / 2 } + fadeIn() togetherWith slideOutHorizontally { it / 2 } + fadeOut()
                    }
                },
                label = "trade_phase"
            ) { currentPhase ->
                when (currentPhase) {
                    0 -> Column(
                        Modifier.fillMaxWidth().padding(horizontal = 20.dp).verticalScroll(rememberScrollState()).padding(bottom = 16.dp)
                    ) {
                        Spacer(Modifier.height(12.dp))
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            OutlinedTextField(
                                value = manualSearchQuery,
                                onValueChange = { manualSearchQuery = it; showManualResult = false },
                                modifier = Modifier.weight(1f),
                                singleLine = true,
                                placeholder = { Text("Search card name for price lookup…", color = dim, fontSize = 13.sp) },
                                leadingIcon = { Icon(Icons.Default.Search, null, tint = amber, modifier = Modifier.size(18.dp)) },
                                trailingIcon = {
                                    if (manualSearchQuery.isNotBlank()) {
                                        Row {
                                            if (!manualSearching) {
                                                IconButton(onClick = {
                                                    manualSearching = true
                                                    showManualResult = true
                                                    onSearchMarketPrice(manualSearchQuery.trim())
                                                }) {
                                                    Icon(Icons.Default.TravelExplore, null, tint = amber, modifier = Modifier.size(20.dp))
                                                }
                                            }
                                            IconButton(onClick = { manualSearchQuery = ""; showManualResult = false; manualSearching = false }) {
                                                Icon(Icons.Default.Close, null, tint = muted, modifier = Modifier.size(16.dp))
                                            }
                                        }
                                    }
                                },
                                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(imeAction = androidx.compose.ui.text.input.ImeAction.Search),
                                keyboardActions = androidx.compose.foundation.text.KeyboardActions(onSearch = {
                                    if (manualSearchQuery.isNotBlank()) { manualSearching = true; showManualResult = true; onSearchMarketPrice(manualSearchQuery.trim()) }
                                }),
                                textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 14.sp),
                                colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = amber, unfocusedBorderColor = border, cursorColor = amber),
                                shape = RoundedCornerShape(10.dp)
                            )
                            Spacer(Modifier.width(6.dp))
                            IconButton(
                                onClick = { showCardScanner = true },
                                modifier = Modifier.size(44.dp)
                            ) {
                                Icon(Icons.Default.CameraAlt, contentDescription = "Scan card", tint = amber, modifier = Modifier.size(22.dp))
                            }
                        }
                        if (showCardScanner) {
                            CardScannerDialog(
                                onDismiss = { showCardScanner = false },
                                onResult = { name ->
                                    manualSearchQuery = name
                                    showCardScanner = false
                                    manualSearching = true
                                    showManualResult = true
                                    onSearchMarketPrice(name.trim())
                                }
                            )
                        }

                        AnimatedVisibility(visible = showManualResult && state.isMarketSearching, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                            manualSearching = true
                            Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                                CircularProgressIndicator(color = amber, strokeWidth = 2.dp, modifier = Modifier.size(18.dp))
                                Spacer(Modifier.width(8.dp))
                                Text("Looking up price…", color = muted, fontSize = 11.sp)
                            }
                        }

                        AnimatedVisibility(visible = showManualResult && !state.isMarketSearching && state.marketSearchResult != null, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                            if (!state.isMarketSearching) manualSearching = false
                            state.marketSearchResult?.let { r ->
                                Surface(color = Color(0xFF0D2818), shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                                    Column(Modifier.padding(12.dp)) {
                                        Text(r.name, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                                        Spacer(Modifier.height(4.dp))
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Text("Mkt $${"%,.2f".format(r.weighted_avg)}", color = muted, fontSize = 10.sp)
                                            Spacer(Modifier.width(8.dp))
                                            Surface(color = green.copy(alpha = 0.15f), shape = RoundedCornerShape(4.dp)) {
                                                Text(" Cash $${"%,.2f".format(r.weighted_avg * state.tradeBuyPct)} ", color = green, fontSize = 10.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp))
                                            }
                                            Spacer(Modifier.width(4.dp))
                                            Surface(color = blue.copy(alpha = 0.15f), shape = RoundedCornerShape(4.dp)) {
                                                Text(" Credit $${"%,.2f".format(r.weighted_avg * state.tradeCreditPct)} ", color = blue, fontSize = 10.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp))
                                            }
                                        }
                                        Spacer(Modifier.height(8.dp))
                                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                            FilledTonalButton(
                                                onClick = { onManualAdd(r.name, r.weighted_avg); manualSearchQuery = ""; showManualResult = false },
                                                colors = ButtonDefaults.filledTonalButtonColors(containerColor = green, contentColor = Color.Black),
                                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                                                modifier = Modifier.height(32.dp),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Icon(Icons.Default.SwapHoriz, null, modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("TRADE", fontWeight = FontWeight.Black, fontSize = 10.sp)
                                            }
                                            FilledTonalButton(
                                                onClick = { onManualAdd(r.name, r.weighted_avg); manualSearchQuery = "" },
                                                colors = ButtonDefaults.filledTonalButtonColors(containerColor = amber, contentColor = Color.Black),
                                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                                                modifier = Modifier.height(32.dp),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Icon(Icons.Default.PlaylistAdd, null, modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("TRADE + NEXT", fontWeight = FontWeight.Black, fontSize = 10.sp)
                                            }
                                            FilledTonalButton(
                                                onClick = { onAddToCart(r.name, r.weighted_avg); manualSearchQuery = ""; showManualResult = false },
                                                colors = ButtonDefaults.filledTonalButtonColors(containerColor = Color(0xFF4ADE80), contentColor = Color.Black),
                                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                                                modifier = Modifier.height(32.dp),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Icon(Icons.Default.AddShoppingCart, null, modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("+ CART", fontWeight = FontWeight.Black, fontSize = 10.sp)
                                            }
                                            FilledTonalButton(
                                                onClick = { onCashOut(r.name, r.weighted_avg) },
                                                colors = ButtonDefaults.filledTonalButtonColors(containerColor = Color(0xFFF59E0B), contentColor = Color.Black),
                                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                                                modifier = Modifier.height(32.dp),
                                                shape = RoundedCornerShape(8.dp)
                                            ) {
                                                Icon(Icons.Default.PointOfSale, null, modifier = Modifier.size(14.dp))
                                                Spacer(Modifier.width(3.dp))
                                                Text("CASH OUT", fontWeight = FontWeight.Black, fontSize = 10.sp)
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        AnimatedVisibility(visible = showManualResult && !state.isMarketSearching && state.marketSearchResult == null && state.marketSearchError.isNotBlank(), enter = fadeIn(), exit = fadeOut()) {
                            manualSearching = false
                            Surface(color = Color(0xFF2A1515), shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                                Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.SearchOff, null, tint = Color(0xFFEF4444), modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("No price found — try a different name", color = Color(0xFFEF4444), fontSize = 11.sp)
                                }
                            }
                        }

                        Spacer(Modifier.height(16.dp))

                        if (state.tradeInItems.isNotEmpty()) {
                            Text("SCANNED CARDS", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                            Spacer(Modifier.height(6.dp))
                            Surface(color = surf, shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                                Column(Modifier.padding(vertical = 4.dp)) {
                                    state.tradeInItems.forEachIndexed { idx, item ->
                                        Row(
                                            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            Surface(color = amber.copy(alpha = 0.12f), shape = RoundedCornerShape(6.dp), modifier = Modifier.size(32.dp)) {
                                                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                                    Text("${idx + 1}", color = amber, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                                }
                                            }
                                            Spacer(Modifier.width(10.dp))
                                            Column(
                                                Modifier.weight(1f).clickable {
                                                    editingItem = item
                                                    editBuyOffer = "%.2f".format(item.buyOffer)
                                                    editTradeCredit = "%.2f".format(item.tradeCredit)
                                                }
                                            ) {
                                                Text(item.product.name, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    if (item.marketPrice > 0) {
                                                        Text("$${"%,.2f".format(item.marketPrice)}", color = muted, fontSize = 10.sp)
                                                        Text(" → ", color = dim, fontSize = 10.sp)
                                                    }
                                                    Text("$${"%,.2f".format(item.buyOffer)}", color = green, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                                    Text(" / ", color = dim, fontSize = 10.sp)
                                                    Text("$${"%,.2f".format(item.tradeCredit)}", color = blue, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                                    Spacer(Modifier.width(4.dp))
                                                    Icon(Icons.Default.Edit, null, tint = dim, modifier = Modifier.size(10.dp))
                                                }
                                            }
                                            IconButton(onClick = { onRemoveItem(item.product.qrCode) }, modifier = Modifier.size(36.dp)) {
                                                Icon(Icons.Default.Delete, null, tint = Color(0xFFEF4444), modifier = Modifier.size(20.dp))
                                            }
                                        }
                                        if (idx < state.tradeInItems.lastIndex) {
                                            HorizontalDivider(Modifier.padding(horizontal = 12.dp), color = border, thickness = 0.5.dp)
                                        }
                                    }
                                }
                            }
                            Spacer(Modifier.height(12.dp))
                        }

                        // ── CUSTOMER GETS (store inventory traded out) ──────────────
                        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                            Text("CUSTOMER GETS (FROM STORE)", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp, modifier = Modifier.weight(1f))
                            TextButton(onClick = { showOutgoingScanner = true }, contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)) {
                                Icon(Icons.Default.QrCodeScanner, null, tint = blue, modifier = Modifier.size(14.dp))
                                Spacer(Modifier.width(4.dp))
                                Text("SCAN", color = blue, fontSize = 10.sp, fontWeight = FontWeight.Black)
                            }
                        }
                        Spacer(Modifier.height(6.dp))
                        if (state.tradeOutItems.isEmpty()) {
                            Surface(color = surf, shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                                Row(Modifier.padding(horizontal = 12.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.Inventory2, null, tint = dim, modifier = Modifier.size(14.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("Scan store inventory cards the customer is taking", color = muted, fontSize = 11.sp, modifier = Modifier.weight(1f))
                                }
                            }
                        } else {
                            Surface(color = surf, shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                                Column(Modifier.padding(vertical = 4.dp)) {
                                    state.tradeOutItems.forEachIndexed { idx, item ->
                                        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                                            Surface(color = blue.copy(alpha = 0.12f), shape = RoundedCornerShape(6.dp), modifier = Modifier.size(32.dp)) {
                                                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                                    Icon(Icons.Default.Inventory2, null, tint = blue, modifier = Modifier.size(14.dp))
                                                }
                                            }
                                            Spacer(Modifier.width(10.dp))
                                            Column(
                                                Modifier.weight(1f).clickable {
                                                    editingOutgoing = item
                                                    editOutgoingPrice = "%.2f".format(item.product.price)
                                                }
                                            ) {
                                                Text(item.product.name, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    Text("$${"%,.2f".format(item.product.price)}", color = blue, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                                    Spacer(Modifier.width(4.dp))
                                                    Icon(Icons.Default.Edit, null, tint = dim, modifier = Modifier.size(10.dp))
                                                }
                                            }
                                            IconButton(onClick = { onRemoveOutgoing(item.product.qrCode) }, modifier = Modifier.size(36.dp)) {
                                                Icon(Icons.Default.Delete, null, tint = Color(0xFFEF4444), modifier = Modifier.size(18.dp))
                                            }
                                        }
                                        if (idx < state.tradeOutItems.lastIndex) {
                                            HorizontalDivider(Modifier.padding(horizontal = 12.dp), color = border, thickness = 0.5.dp)
                                        }
                                    }
                                    HorizontalDivider(Modifier.padding(horizontal = 12.dp), color = border, thickness = 0.5.dp)
                                    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
                                        Text("Inventory subtotal", color = muted, fontSize = 11.sp, modifier = Modifier.weight(1f))
                                        Text("−$${"%,.2f".format(outgoingTotal)}", color = blue, fontSize = 12.sp, fontWeight = FontWeight.Black)
                                    }
                                }
                            }
                        }
                        if (showOutgoingScanner) {
                            CompactQrScannerDialog(
                                prompt = "Scan store inventory card customer is taking",
                                onDismiss = { showOutgoingScanner = false },
                                onResult = { code ->
                                    onScanOutgoing(code.trim())
                                    showOutgoingScanner = false
                                }
                            )
                        }
                        Spacer(Modifier.height(12.dp))

                        // ── Recently Canceled (recovery list) ───────────────────────
                        if (state.recentCanceledTrades.isNotEmpty()) {
                            Surface(
                                onClick = { showRecentCanceled = !showRecentCanceled },
                                color = surf,
                                shape = RoundedCornerShape(10.dp),
                                border = BorderStroke(0.5.dp, border),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.History, null, tint = amber, modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("Recently Canceled (${state.recentCanceledTrades.size})", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                                    Icon(if (showRecentCanceled) Icons.Default.ExpandLess else Icons.Default.ExpandMore, null, tint = muted, modifier = Modifier.size(18.dp))
                                }
                            }
                            AnimatedVisibility(visible = showRecentCanceled, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                                Column(Modifier.padding(top = 6.dp)) {
                                    state.recentCanceledTrades.forEach { snap ->
                                        val total = snap.incoming.size + snap.outgoing.size
                                        val ageMin = ((System.currentTimeMillis() - snap.canceledAt) / 60000).coerceAtLeast(0)
                                        Surface(color = surf2, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
                                            Row(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                                                Column(Modifier.weight(1f)) {
                                                    Text(
                                                        if (snap.customerName.isNotBlank()) snap.customerName else "Unnamed customer",
                                                        color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Medium
                                                    )
                                                    Text("$total item${if (total == 1) "" else "s"} • ${if (ageMin == 0L) "just now" else "${ageMin}m ago"}", color = muted, fontSize = 9.sp)
                                                }
                                                TextButton(onClick = { onRestoreCanceled(snap.id) }, contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp)) {
                                                    Icon(Icons.Default.Restore, null, tint = green, modifier = Modifier.size(14.dp))
                                                    Spacer(Modifier.width(4.dp))
                                                    Text("RESTORE", color = green, fontSize = 10.sp, fontWeight = FontWeight.Black)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Spacer(Modifier.height(12.dp))
                        }

                        if (state.widgetTradeCredit > 0f) {
                            Surface(color = Color(0xFF0D2818), shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth()) {
                                Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.Widgets, null, tint = green, modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("Widget session", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                                    Text("$${"%,.2f".format(widgetCreditD * (8.0 / 9.0))}", color = green, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    Text(" / ", color = dim, fontSize = 11.sp)
                                    Text("$${"%,.2f".format(widgetCreditD)}", color = blue, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                }
                            }
                            Spacer(Modifier.height(12.dp))
                        }

                        if (hasItems) {
                            Text("ADDITIONAL CREDIT", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                            Spacer(Modifier.height(6.dp))
                            OutlinedTextField(
                                value = manualCreditInput,
                                onValueChange = { v ->
                                    if (v.isEmpty() || v.matches(Regex("^\\d{0,6}(\\.\\d{0,2})?\$"))) {
                                        manualCreditInput = v; onSetManualCredit(v.toFloatOrNull() ?: 0f)
                                    }
                                },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                placeholder = { Text("0.00", color = dim, fontSize = 13.sp) },
                                prefix = { Text("$", color = amber, fontWeight = FontWeight.Bold) },
                                textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.Bold),
                                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                                colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = amber, unfocusedBorderColor = border, cursorColor = amber),
                                shape = RoundedCornerShape(10.dp),
                                trailingIcon = if (manualCreditInput.isNotEmpty()) { { IconButton(onClick = { manualCreditInput = ""; onSetManualCredit(0f) }) { Icon(Icons.Default.Close, null, tint = muted, modifier = Modifier.size(16.dp)) } } } else null
                            )
                            Spacer(Modifier.height(12.dp))
                        }

                        Surface(
                            onClick = { showQuickEstimate = !showQuickEstimate },
                            color = if (showQuickEstimate) amber.copy(alpha = 0.08f) else surf,
                            shape = RoundedCornerShape(10.dp),
                            border = BorderStroke(0.5.dp, if (showQuickEstimate) amber.copy(alpha = 0.3f) else border),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Calculate, null, tint = amber, modifier = Modifier.size(16.dp))
                                Spacer(Modifier.width(8.dp))
                                Text("Quick Estimate", color = if (showQuickEstimate) amber else Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                                Spacer(Modifier.weight(1f))
                                Icon(
                                    if (showQuickEstimate) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                                    null, tint = muted, modifier = Modifier.size(18.dp)
                                )
                            }
                        }
                        AnimatedVisibility(visible = showQuickEstimate, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                            Column(Modifier.padding(top = 8.dp)) {
                                OutlinedTextField(
                                    value = quickPrice,
                                    onValueChange = { v -> if (v.isEmpty() || v.matches(Regex("^\\d{0,6}(\\.\\d{0,2})?\$"))) quickPrice = v },
                                    modifier = Modifier.fillMaxWidth(),
                                    singleLine = true,
                                    placeholder = { Text("Market price…", color = dim, fontSize = 14.sp) },
                                    prefix = { Text("$", color = amber, fontWeight = FontWeight.Bold, fontSize = 18.sp) },
                                    textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Bold),
                                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Decimal),
                                    colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = amber, unfocusedBorderColor = border, cursorColor = amber),
                                    shape = RoundedCornerShape(10.dp)
                                )
                                Spacer(Modifier.height(8.dp))
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                    listOf(5f, 10f, 25f, 50f, 100f, 250f).forEach { amt ->
                                        Surface(
                                            onClick = { quickPrice = String.format("%.2f", amt) },
                                            color = surf,
                                            shape = RoundedCornerShape(6.dp),
                                            border = BorderStroke(0.5.dp, border),
                                            modifier = Modifier.weight(1f)
                                        ) {
                                            Box(Modifier.padding(vertical = 6.dp), contentAlignment = Alignment.Center) {
                                                Text("\$${amt.toInt()}", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                            }
                                        }
                                    }
                                }
                                AnimatedVisibility(visible = quickParsed > 0f, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                                    Surface(color = surf, shape = RoundedCornerShape(10.dp), modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                                        Row(Modifier.padding(16.dp), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
                                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                                Text("CASH ${buyPctInt}%", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp)
                                                Spacer(Modifier.height(2.dp))
                                                Text("$${"%,.2f".format(quickCash)}", color = green, fontSize = 22.sp, fontWeight = FontWeight.Black)
                                            }
                                            Box(Modifier.width(1.dp).height(40.dp).background(border))
                                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                                Text("CREDIT ${creditPctInt}%", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp)
                                                Spacer(Modifier.height(2.dp))
                                                Text("$${"%,.2f".format(quickCredit)}", color = blue, fontSize = 22.sp, fontWeight = FontWeight.Black)
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        Spacer(Modifier.height(16.dp))

                        if (hasItems) {
                            val reviewPulse = rememberInfiniteTransition(label = "reviewPulse")
                            val reviewScale by reviewPulse.animateFloat(
                                initialValue = 1f, targetValue = 1.04f,
                                animationSpec = infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "rvs"
                            )
                            val reviewGlow by reviewPulse.animateFloat(
                                initialValue = 0.85f, targetValue = 1f,
                                animationSpec = infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "rvg"
                            )
                            Button(
                                onClick = { phase = 1 },
                                modifier = Modifier.fillMaxWidth().height(52.dp)
                                    .graphicsLayer(scaleX = reviewScale, scaleY = reviewScale),
                                colors = ButtonDefaults.buttonColors(containerColor = amber.copy(alpha = reviewGlow)),
                                shape = RoundedCornerShape(12.dp)
                            ) {
                                Text("REVIEW OFFER", color = Color.Black, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 0.5.sp)
                                Spacer(Modifier.width(8.dp))
                                Icon(Icons.Default.ArrowForward, null, tint = Color.Black, modifier = Modifier.size(18.dp))
                            }
                            Spacer(Modifier.height(8.dp))
                        }

                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                            TextButton(onClick = { if (hasItems) onCancel() else onDismiss() }) {
                                Text(if (hasItems) "Cancel Trade" else "Close", color = muted, fontSize = 11.sp)
                            }
                            if (hasItems) {
                                TextButton(onClick = onDismiss) {
                                    Text("Hide (keep scanning)", color = muted, fontSize = 11.sp)
                                }
                            }
                        }
                    }

                    1 -> Column(
                        Modifier.fillMaxWidth().padding(horizontal = 20.dp).verticalScroll(rememberScrollState()).padding(bottom = 16.dp)
                    ) {
                        Spacer(Modifier.height(12.dp))

                        // ── Totals summary ───────────────────────────────────────────
                        Surface(color = surf, shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth()) {
                            Row(Modifier.padding(16.dp), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Text("CASH OFFER", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    Spacer(Modifier.height(4.dp))
                                    Text("$${"%,.2f".format(cashTotal)}", color = green, fontSize = 24.sp, fontWeight = FontWeight.Black)
                                    Text("${buyPctInt}% of market", color = muted, fontSize = 9.sp)
                                }
                                Box(Modifier.width(1.dp).height(50.dp).background(border))
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Text("STORE CREDIT", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    Spacer(Modifier.height(4.dp))
                                    Text("$${"%,.2f".format(creditTotal)}", color = blue, fontSize = 24.sp, fontWeight = FontWeight.Black)
                                    Text("${creditPctInt}% of market", color = muted, fontSize = 9.sp)
                                }
                            }
                        }

                        // ── Customer name ─────────────────────────────────────────────
                        Spacer(Modifier.height(16.dp))
                        Text("CUSTOMER (OPTIONAL)", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                        Spacer(Modifier.height(6.dp))
                        OutlinedTextField(
                            value = customerName,
                            onValueChange = { customerName = it },
                            placeholder = { Text("Walk-in", color = dim, fontSize = 13.sp) },
                            singleLine = true,
                            colors = OutlinedTextFieldDefaults.colors(focusedBorderColor = green, unfocusedBorderColor = border, focusedTextColor = Color.White, unfocusedTextColor = Color.White, cursorColor = green),
                            leadingIcon = { Icon(Icons.Default.Person, null, tint = green, modifier = Modifier.size(18.dp)) },
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp)
                        )

                        // ── Choose path ───────────────────────────────────────────────
                        Spacer(Modifier.height(20.dp))
                        Text("CHOOSE HOW TO PROCEED", color = muted, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                        Spacer(Modifier.height(10.dp))

                        // Option 1 — Cash Out
                        Surface(
                            onClick = {
                                // Snapshot items SYNCHRONOUSLY on the main thread BEFORE dispatching
                                // the async finalize coroutine — this eliminates the race that caused
                                // the signing screen to show $0.00.
                                val itemsSnapshot = state.tradeInItems.toList()
                                onRequestSigning(itemsSnapshot) { onDismiss() }
                                onFinalize(customerName.trim(), TradeOfferType.CASH)
                            },
                            color = green.copy(alpha = 0.08f),
                            shape = RoundedCornerShape(12.dp),
                            border = BorderStroke(1.5.dp, green.copy(alpha = 0.4f)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.AttachMoney, null, tint = green, modifier = Modifier.size(28.dp))
                                Spacer(Modifier.width(14.dp))
                                Column(Modifier.weight(1f)) {
                                    Text("CASH OUT", color = green, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 0.5.sp)
                                    Text("Pay customer $${"%,.2f".format(cashTotal)} cash • sign & complete", color = muted, fontSize = 11.sp)
                                }
                                Icon(Icons.Default.ChevronRight, null, tint = green, modifier = Modifier.size(20.dp))
                            }
                        }

                        Spacer(Modifier.height(8.dp))

                        // Option 2 — Store Credit
                        Surface(
                            onClick = {
                                val itemsSnapshot = state.tradeInItems.toList()
                                onRequestSigning(itemsSnapshot) { onDismiss() }
                                onFinalize(customerName.trim(), TradeOfferType.STORE_CREDIT)
                            },
                            color = blue.copy(alpha = 0.08f),
                            shape = RoundedCornerShape(12.dp),
                            border = BorderStroke(1.5.dp, blue.copy(alpha = 0.4f)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.CardGiftcard, null, tint = blue, modifier = Modifier.size(28.dp))
                                Spacer(Modifier.width(14.dp))
                                Column(Modifier.weight(1f)) {
                                    Text("STORE CREDIT", color = blue, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 0.5.sp)
                                    Text("Give $${"%,.2f".format(creditTotal)} credit • sign & record", color = muted, fontSize = 11.sp)
                                }
                                Icon(Icons.Default.ChevronRight, null, tint = blue, modifier = Modifier.size(20.dp))
                            }
                        }

                        Spacer(Modifier.height(8.dp))

                        // Option 3 — Apply to Cart & Buy Items
                        Surface(
                            onClick = {
                                val itemsSnapshot = state.tradeInItems.toList()
                                onRequestSigning(itemsSnapshot) { onDismiss() }
                                onApplyToCart(customerName.trim())
                            },
                            color = amber.copy(alpha = 0.08f),
                            shape = RoundedCornerShape(12.dp),
                            border = BorderStroke(1.5.dp, amber.copy(alpha = 0.4f)),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.ShoppingCart, null, tint = amber, modifier = Modifier.size(28.dp))
                                Spacer(Modifier.width(14.dp))
                                Column(Modifier.weight(1f)) {
                                    Text("BUY ITEMS FROM STORE", color = amber, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 0.5.sp)
                                    Text("Apply $${"%,.2f".format(creditTotal)} credit • sign • then scan inventory", color = muted, fontSize = 11.sp)
                                }
                                Icon(Icons.Default.ChevronRight, null, tint = amber, modifier = Modifier.size(20.dp))
                            }
                        }

                        // ── Signature note ────────────────────────────────────────────
                        Spacer(Modifier.height(12.dp))
                        Surface(Modifier.fillMaxWidth(), color = Color(0xFF1A1A1A), shape = RoundedCornerShape(8.dp)) {
                            Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Draw, null, tint = muted, modifier = Modifier.size(14.dp))
                                Spacer(Modifier.width(8.dp))
                                Text("Customer signs disclosure before completing any option • 2 copies printed automatically", color = muted, fontSize = 10.sp, lineHeight = 14.sp)
                            }
                        }

                        Spacer(Modifier.height(16.dp))
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                            TextButton(onClick = { phase = 0 }) {
                                Icon(Icons.Default.ArrowBack, null, tint = muted, modifier = Modifier.size(14.dp))
                                Spacer(Modifier.width(4.dp))
                                Text("Back to cards", color = muted, fontSize = 11.sp)
                            }
                            TextButton(onClick = onCancel) {
                                Text("Cancel Trade", color = Color(0xFFEF4444).copy(alpha = 0.7f), fontSize = 11.sp)
                            }
                        }
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────────────
// Lot Speed Evaluator Sheet
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LotEvaluatorSheet(
    items: List<LotEvalItem>,
    isActive: Boolean,
    onDismiss: () -> Unit,
    onToggleMode: () -> Unit,
    onRemoveItem: (String) -> Unit,
    onClear: () -> Unit,
    onConvertToTradeIn: () -> Unit
) {
    val green = Color(0xFF4ADE80)
    val amber = Color(0xFFF59E0B)
    val red = Color(0xFFFF6B6B)

    fun categoryColor(cat: LotCategory) = when (cat) {
        LotCategory.FLAG     -> red
        LotCategory.VALUE    -> green
        LotCategory.STANDARD -> amber
        LotCategory.BULK     -> Color(0xFF555555)
    }
    fun categoryLabel(cat: LotCategory) = when (cat) {
        LotCategory.FLAG     -> "FLAG"
        LotCategory.VALUE    -> "VALUE"
        LotCategory.STANDARD -> "STD"
        LotCategory.BULK     -> "BULK"
    }

    val flagItems     = items.filter { it.category == LotCategory.FLAG }
    val valueItems    = items.filter { it.category == LotCategory.VALUE }
    val standardItems = items.filter { it.category == LotCategory.STANDARD }
    val bulkItems     = items.filter { it.category == LotCategory.BULK }

    val valuableTotal = (flagItems + valueItems).sumOf { it.marketPrice }
    val standardTotal = standardItems.sumOf { it.marketPrice }
    val bulkCashTotal = bulkItems.size * 0.10

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        containerColor = Color(0xFF181818),
        dragHandle = null,
        windowInsets = WindowInsets(0)
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp)
                .navigationBarsPadding()
                .padding(bottom = 16.dp)
        ) {
            Spacer(Modifier.height(16.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.ViewList, null, tint = green, modifier = Modifier.size(20.dp))
                Spacer(Modifier.width(8.dp))
                Text("Lot Speed Evaluator", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Spacer(Modifier.weight(1f))
                Surface(
                    onClick = onToggleMode,
                    color = if (isActive) Color(0xFF0A2A0A) else Color(0xFF1A1A1A),
                    shape = RoundedCornerShape(6.dp)
                ) {
                    Text(
                        if (isActive) "● SCANNING" else "PAUSED",
                        color = if (isActive) green else Color(0xFF555555),
                        fontSize = 10.sp, fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp)
                    )
                }
            }
            Text(
                "Scan cards rapidly — each is auto-categorized by market price.",
                color = Color(0xFF666666), fontSize = 11.sp,
                modifier = Modifier.padding(top = 4.dp, bottom = 12.dp)
            )
            HorizontalDivider(color = Color(0xFF2A2A2A))
            Spacer(Modifier.height(12.dp))

            if (items.isEmpty()) {
                Box(Modifier.fillMaxWidth().padding(vertical = 24.dp), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Default.QrCodeScanner, null, tint = Color(0xFF333333), modifier = Modifier.size(36.dp))
                        Spacer(Modifier.height(8.dp))
                        Text("Start scanning cards to evaluate the lot", color = Color(0xFF555555), fontSize = 13.sp)
                        Spacer(Modifier.height(4.dp))
                        Text("FLAG ≥\$30  •  VALUE \$5–\$29  •  STD \$1–\$4  •  BULK <\$1", color = Color(0xFF444444), fontSize = 10.sp)
                    }
                }
            } else {
                // Summary row
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                    @Composable fun SummaryChip(label: String, count: Int, total: Double?, color: Color) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally,
                            modifier = Modifier.background(Color(0xFF111111), RoundedCornerShape(8.dp)).padding(10.dp)) {
                            Text(label, color = color, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                            Text("$count", color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Black)
                            if (total != null) Text("${"$%.2f".format(total)}", color = Color(0xFF888888), fontSize = 10.sp)
                        }
                    }
                    SummaryChip("FLAG", flagItems.size, flagItems.sumOf { it.marketPrice }, red)
                    SummaryChip("VALUE", valueItems.size, valueItems.sumOf { it.marketPrice }, green)
                    SummaryChip("STD", standardItems.size, standardTotal, amber)
                    SummaryChip("BULK", bulkItems.size, bulkCashTotal, Color(0xFF555555))
                }
                Spacer(Modifier.height(12.dp))
                HorizontalDivider(color = Color(0xFF2A2A2A))
                Spacer(Modifier.height(8.dp))

                // Card list
                Column(Modifier.fillMaxWidth().heightIn(max = 220.dp).verticalScroll(rememberScrollState())) {
                    items.reversed().forEach { item ->
                        val catColor = categoryColor(item.category)
                        Row(
                            Modifier.fillMaxWidth().padding(vertical = 5.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Surface(color = catColor.copy(alpha = 0.15f), shape = RoundedCornerShape(4.dp)) {
                                Text(categoryLabel(item.category), color = catColor, fontSize = 9.sp, fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 3.dp))
                            }
                            Spacer(Modifier.width(8.dp))
                            Text(item.product.name, color = Color.White, fontSize = 12.sp,
                                modifier = Modifier.weight(1f),
                                maxLines = 1, overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis)
                            Text(
                                if (item.marketPrice > 0) "${"$%.2f".format(item.marketPrice)}" else "???",
                                color = catColor, fontSize = 12.sp, fontWeight = FontWeight.SemiBold
                            )
                            Spacer(Modifier.width(4.dp))
                            IconButton(onClick = { onRemoveItem(item.product.qrCode) }, modifier = Modifier.size(24.dp)) {
                                Icon(Icons.Default.Close, null, tint = Color(0xFF444444), modifier = Modifier.size(14.dp))
                            }
                        }
                    }
                }
                Spacer(Modifier.height(8.dp))
                HorizontalDivider(color = Color(0xFF2A2A2A))
                Spacer(Modifier.height(12.dp))

                // Total row
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("Est. offer value (90% credit)", color = Color(0xFF888888), fontSize = 12.sp, modifier = Modifier.weight(1f))
                    Text("${"$%.2f".format((valuableTotal + standardTotal + bulkCashTotal) * 0.90)}", color = green, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.height(12.dp))

                // Action buttons
                if (flagItems.isNotEmpty() || valueItems.isNotEmpty()) {
                    Button(
                        onClick = onConvertToTradeIn,
                        modifier = Modifier.fillMaxWidth(),
                        colors = ButtonDefaults.buttonColors(containerColor = green)
                    ) {
                        Icon(Icons.Default.SwapHoriz, null, tint = Color.Black, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Move ${flagItems.size + valueItems.size} valuables to Trade-In", color = Color.Black, fontWeight = FontWeight.Bold)
                    }
                    Spacer(Modifier.height(8.dp))
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = onClear, modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = red),
                        border = BorderStroke(1.dp, Color(0xFF3A0A0A))) {
                        Text("Clear Lot", fontWeight = FontWeight.Bold)
                    }
                    OutlinedButton(onClick = onDismiss, modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = Color(0xFF888888)),
                        border = BorderStroke(1.dp, Color(0xFF2A2A2A))) {
                        Text("Close")
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────────────
// Arbitrage Scout Sheet
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ArbitrageScoutSheet(
    repriceQueue: List<RepriceSuggestion>,
    onDismiss: () -> Unit,
    onUpdatePrice: (qrCode: String, price: Double) -> Unit,
    onUpdateAll: () -> Unit
) {
    val green = Color(0xFF4ADE80)
    val amber = Color(0xFFF59E0B)
    val bg = Color(0xFF131313)
    val surf = Color(0xFF1A1A1A)
    val border = Color(0xFF2A2A2A)
    val underpriced = repriceQueue.filter { it.pctChange > 0 }.sortedByDescending { it.suggestedPrice - it.product.price }
    val totalLeftOnTable = underpriced.sumOf { it.suggestedPrice - it.product.price }
    val avgPctUp = if (underpriced.isNotEmpty()) underpriced.map { it.pctChange }.average() else 0.0

    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = bg,
        dragHandle = null,
        windowInsets = WindowInsets(0)
    ) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(0.85f).navigationBarsPadding()) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(top = 16.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.AttachMoney, null, tint = green, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("Arbitrage Scout", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    Spacer(Modifier.weight(1f))
                    IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                        Icon(Icons.Default.Close, null, tint = Color(0xFF666666), modifier = Modifier.size(16.dp))
                    }
                }
                Text(
                    "Your prices are below current market. Raise them to capture the difference.",
                    color = Color(0xFF666666), fontSize = 11.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 14.dp)
                )

                val revPulse = rememberInfiniteTransition(label = "revPulse")
                val revGlow by revPulse.animateFloat(
                    initialValue = 0.6f, targetValue = 1f,
                    animationSpec = infiniteRepeatable(tween(1000, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "rg"
                )
                val revScale by revPulse.animateFloat(
                    initialValue = 1f, targetValue = 1.05f,
                    animationSpec = infiniteRepeatable(tween(1000, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "rs"
                )
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Surface(Modifier.weight(1f), color = green.copy(alpha = 0.08f), shape = RoundedCornerShape(10.dp), border = BorderStroke(1.dp, green.copy(alpha = revGlow * 0.3f))) {
                        Column(Modifier.padding(14.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("REVENUE OPPORTUNITY", color = Color(0xFF666666), fontSize = 8.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                            Spacer(Modifier.height(4.dp))
                            Text("${"$%,.2f".format(totalLeftOnTable)}", color = green.copy(alpha = revGlow), fontWeight = FontWeight.Black, fontSize = 22.sp, modifier = Modifier.graphicsLayer(scaleX = revScale, scaleY = revScale))
                        }
                    }
                    Column(Modifier.weight(0.6f), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Surface(Modifier.fillMaxWidth(), color = surf, shape = RoundedCornerShape(8.dp)) {
                            Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                                Text("CARDS", color = Color(0xFF666666), fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                Text("${underpriced.size}", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                            }
                        }
                        Surface(Modifier.fillMaxWidth(), color = surf, shape = RoundedCornerShape(8.dp)) {
                            Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                                Text("AVG GAP", color = Color(0xFF666666), fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                Text("+${"%.0f".format(avgPctUp)}%", color = green, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                            }
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))
            }
            HorizontalDivider(color = border)

            if (underpriced.isEmpty()) {
                Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Default.CheckCircle, null, tint = Color(0xFF333333), modifier = Modifier.size(40.dp))
                        Spacer(Modifier.height(8.dp))
                        Text("No underpriced cards detected — prices look good!", color = Color(0xFF555555), fontSize = 13.sp)
                    }
                }
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f).padding(horizontal = 20.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                    contentPadding = PaddingValues(vertical = 8.dp)
                ) {
                    items(underpriced.size, key = { underpriced[it].product.qrCode }) { index ->
                        val suggestion = underpriced[index]
                        val gap = suggestion.suggestedPrice - suggestion.product.price
                        val cond = suggestion.condition
                        val condColor = when (cond) {
                            "NM" -> green; "LP" -> amber; "MP" -> Color(0xFFFF8C42); "HP" -> Color(0xFFFF5555); "DMG" -> Color(0xFFAA3333); else -> Color(0xFF888888)
                        }
                        val itemVisible = remember { Animatable(0f) }
                        LaunchedEffect(Unit) { itemVisible.animateTo(1f, tween(300, delayMillis = index * 30)) }
                        Surface(
                            Modifier.fillMaxWidth()
                                .graphicsLayer(alpha = itemVisible.value, translationX = (1f - itemVisible.value) * 40f),
                            color = surf, shape = RoundedCornerShape(10.dp)
                        ) {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Surface(color = green.copy(alpha = 0.1f), shape = RoundedCornerShape(8.dp), modifier = Modifier.size(44.dp)) {
                                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                            Text("+${"$%.0f".format(gap)}", color = green, fontSize = 11.sp, fontWeight = FontWeight.Black)
                                            Text("+${"%.0f".format(suggestion.pctChange)}%", color = green.copy(alpha = 0.6f), fontSize = 8.sp)
                                        }
                                    }
                                }
                                Spacer(Modifier.width(10.dp))
                                Column(Modifier.weight(1f)) {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        Text(suggestion.product.name, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
                                        Spacer(Modifier.width(6.dp))
                                        Surface(color = condColor.copy(alpha = 0.18f), shape = RoundedCornerShape(3.dp)) {
                                            Text(cond, color = condColor, fontSize = 9.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                        }
                                    }
                                    Spacer(Modifier.height(3.dp))
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        Text("${"$%.2f".format(suggestion.product.price)}", color = Color(0xFF888888), fontSize = 11.sp)
                                        Icon(Icons.Default.ArrowForward, null, tint = Color(0xFF444444), modifier = Modifier.size(14.dp).padding(horizontal = 2.dp))
                                        Text("${"$%.2f".format(suggestion.suggestedPrice)}", color = green, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    }
                                }
                                Spacer(Modifier.width(8.dp))
                                Button(
                                    onClick = { onUpdatePrice(suggestion.product.qrCode, suggestion.suggestedPrice) },
                                    colors = ButtonDefaults.buttonColors(containerColor = green.copy(alpha = 0.15f)),
                                    contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                                    modifier = Modifier.height(34.dp),
                                    shape = RoundedCornerShape(8.dp)
                                ) {
                                    Icon(Icons.Default.TrendingUp, null, tint = green, modifier = Modifier.size(14.dp))
                                    Spacer(Modifier.width(4.dp))
                                    Text("Raise", color = green, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                }
                            }
                        }
                    }
                }

                Column(Modifier.fillMaxWidth().background(bg).padding(horizontal = 20.dp).padding(bottom = 16.dp, top = 8.dp)) {
                    val raiseAllPulse = rememberInfiniteTransition(label = "raiseAllPulse")
                    val raiseScale by raiseAllPulse.animateFloat(
                        initialValue = 1f, targetValue = 1.03f,
                        animationSpec = infiniteRepeatable(tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "ras"
                    )
                    val raiseBorderAlpha by raiseAllPulse.animateFloat(
                        initialValue = 0.3f, targetValue = 0.9f,
                        animationSpec = infiniteRepeatable(tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse), label = "rba"
                    )
                    Button(
                        onClick = { onUpdateAll(); onDismiss() },
                        modifier = Modifier.fillMaxWidth().height(48.dp)
                            .graphicsLayer(scaleX = raiseScale, scaleY = raiseScale)
                            .border(1.5.dp, green.copy(alpha = raiseBorderAlpha), RoundedCornerShape(10.dp)),
                        colors = ButtonDefaults.buttonColors(containerColor = green),
                        shape = RoundedCornerShape(10.dp)
                    ) {
                        Icon(Icons.Default.AttachMoney, null, tint = Color.Black, modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Raise All ${underpriced.size} Prices — Capture ${"$%,.2f".format(totalLeftOnTable)}", color = Color.Black, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────────────
// Market Price Search Sheet
// ─────────────────────────────────────────────────────────────────────────────────────
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MarketPriceSearchSheet(
    state: POSViewState,
    onDismiss: () -> Unit,
    onSearch: (query: String, setCode: String, cardNumber: String) -> Unit,
    onLoadVariants: (query: String) -> Unit,
    onSelectVariant: (idx: Int) -> Unit,
    onSetLanguage: (language: String) -> Unit,
    onClear: () -> Unit,
    cacheSize: Int = 0
) {
    val gold = Color(0xFFD4AF37)
    val bg = Color(0xFF121212)
    var query by remember { mutableStateOf(state.marketSearchQuery) }
    var setQuery by remember { mutableStateOf("") }
    var cardNumberQuery by remember { mutableStateOf("") }
    val result = state.marketSearchResult
    val isSearching = state.isMarketSearching
    val variants = state.marketVariants
    val isLoadingVariants = state.isLoadingVariants
    val selectedIdx = state.selectedVariantIdx
    val activeLang = state.marketLanguage
    val scrollState = rememberScrollState()
    val keyboard = androidx.compose.ui.platform.LocalSoftwareKeyboardController.current
    var searchError by remember { mutableStateOf("") }
    // Guard: only show "no result" error after the user has actually triggered a search,
    // not on initial composition (which fires LaunchedEffect with isSearching=false).
    var hasSearched by remember { mutableStateOf(false) }
    var showCardScanner by remember { mutableStateOf(false) }

    fun doSearch() {
        if (query.isNotBlank()) {
            keyboard?.hide()
            searchError = ""
            hasSearched = true
            onSearch(query.trim(), setQuery.trim(), cardNumberQuery.trim())
        }
    }

    LaunchedEffect(query) {
        if (query.length >= 2) {
            delay(300)
            onLoadVariants(query.trim())
        }
    }

    // Auto-fill set + number when user selects a variant from the picker.
    // The variant tap also triggers an auto-search via the ViewModel, so mark hasSearched.
    LaunchedEffect(selectedIdx) {
        if (selectedIdx >= 0 && selectedIdx < variants.size) {
            val v = variants[selectedIdx]
            if (v.set_name.isNotBlank()) setQuery = v.set_name
            if (v.number.isNotBlank()) cardNumberQuery = v.number
            hasSearched = true
        }
    }

    // Auto-scroll to show result when it arrives, and clear any inline error.
    // Use a generous delay so the layout has time to remeasure before we scroll.
    LaunchedEffect(result) {
        if (result != null) {
            searchError = ""
            delay(400)
            scrollState.animateScrollTo(scrollState.maxValue)
        }
    }

    // Track search completion to show inline error if result is null.
    // Only fires after the user has actually pressed Search (hasSearched guard prevents
    // false-positives on initial composition when isSearching is already false).
    LaunchedEffect(isSearching) {
        if (!isSearching && hasSearched && result == null) {
            searchError = if (state.marketSearchError.isNotEmpty())
                state.marketSearchError
            else
                "No result — check card name or try a different spelling"
        }
    }

    val langOptions = listOf(
        "EN" to "🇺🇸 EN",
        "JP" to "🇯🇵 JP",
        "KR" to "🇰🇷 KR",
        "CN" to "🇨🇳 CN"
    )
    val langDiscount = state.serverLangFactors.mapValues { it.value.toInt() }
    val langFactor = if (activeLang == "EN") 1.0 else 1.0 - (langDiscount[activeLang] ?: 0) / 100.0

    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = bg,
        tonalElevation = 0.dp,
        dragHandle = null,
        windowInsets = WindowInsets(0)
    ) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(0.92f).verticalScroll(scrollState).padding(horizontal = 20.dp).padding(top = 16.dp, bottom = 32.dp)) {
            // ── Header ────────────────────────────────────────────────────────────
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Search, null, tint = gold, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text("Market Price Search", color = gold, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Spacer(Modifier.weight(1f))
                if (cacheSize > 0) {
                    Surface(color = Color(0xFF1A2A1A), shape = RoundedCornerShape(4.dp)) {
                        Row(Modifier.padding(horizontal = 7.dp, vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.FlashOn, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(9.dp))
                            Spacer(Modifier.width(3.dp))
                            Text("$cacheSize cached", color = Color(0xFF4ADE80), fontSize = 9.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                    Spacer(Modifier.width(8.dp))
                }
                IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                    Icon(Icons.Default.Close, null, tint = Color(0xFF666666), modifier = Modifier.size(16.dp))
                }
            }
            Spacer(Modifier.height(14.dp))

            // ── Language selector ─────────────────────────────────────────────────
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                langOptions.forEach { (code, label) ->
                    val selected = activeLang == code
                    Surface(
                        onClick = { onSetLanguage(code) },
                        color = if (selected) gold.copy(alpha = 0.15f) else Color(0xFF1A1A1A),
                        shape = RoundedCornerShape(6.dp),
                        border = if (selected) BorderStroke(1.dp, gold) else BorderStroke(1.dp, Color(0xFF2A2A2A)),
                        modifier = Modifier.weight(1f)
                    ) {
                        Column(Modifier.padding(vertical = 6.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(label, color = if (selected) gold else Color(0xFF888888), fontSize = 11.sp, fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal)
                            if (code != "EN") {
                                val disc = langDiscount[code] ?: 0
                                Text("−$disc%", color = if (selected) Color(0xFFF59E0B) else Color(0xFF555555), fontSize = 8.sp)
                            }
                        }
                    }
                }
            }
            Spacer(Modifier.height(14.dp))

            // ── Card name field ───────────────────────────────────────────────────
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    label = { Text("Card Name  *required", color = Color(0xFF666666)) },
                    placeholder = { Text("e.g. Charizard ex", color = Color(0xFF444444)) },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedBorderColor = gold,
                        unfocusedBorderColor = Color(0xFF333333),
                        cursorColor = gold
                    ),
                    keyboardActions = KeyboardActions(onSearch = { doSearch() }, onNext = {}),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search)
                )
                Spacer(Modifier.width(6.dp))
                IconButton(
                    onClick = { showCardScanner = true },
                    modifier = Modifier.size(48.dp)
                ) {
                    Icon(Icons.Default.CameraAlt, contentDescription = "Scan card", tint = gold, modifier = Modifier.size(24.dp))
                }
            }
            if (showCardScanner) {
                CardScannerDialog(
                    onDismiss = { showCardScanner = false },
                    onResult = { name ->
                        query = name
                        showCardScanner = false
                        hasSearched = true
                        onSearch(name.trim(), setQuery.trim(), cardNumberQuery.trim())
                    }
                )
            }
            // ── Recent search history chips ───────────────────────────────────────
            if (state.marketSearchHistory.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Row(
                    modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    state.marketSearchHistory.forEach { term ->
                        Surface(
                            onClick = {
                                query = term
                                hasSearched = true
                                onSearch(term, "", "")
                            },
                            color = Color(0xFF1A1A2A),
                            shape = RoundedCornerShape(16.dp),
                            border = BorderStroke(1.dp, Color(0xFF3A3A5A))
                        ) {
                            Row(
                                Modifier.padding(horizontal = 10.dp, vertical = 5.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Icon(
                                    Icons.Default.History,
                                    contentDescription = null,
                                    tint = Color(0xFF888888),
                                    modifier = Modifier.size(11.dp)
                                )
                                Spacer(Modifier.width(4.dp))
                                Text(term, color = Color(0xFFBBBBBB), fontSize = 11.sp, maxLines = 1)
                            }
                        }
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
            // ── Set name + card number (optional refiners) ────────────────────────
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = setQuery,
                    onValueChange = { setQuery = it },
                    label = { Text("Set Name  (optional)", color = Color(0xFF666666)) },
                    placeholder = { Text("e.g. Paldea Evolved", color = Color(0xFF444444)) },
                    singleLine = true,
                    modifier = Modifier.weight(1.6f),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedBorderColor = gold.copy(alpha = 0.6f),
                        unfocusedBorderColor = Color(0xFF2A2A2A),
                        cursorColor = gold
                    ),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next)
                )
                OutlinedTextField(
                    value = cardNumberQuery,
                    onValueChange = { cardNumberQuery = it },
                    label = { Text("Card #", color = Color(0xFF666666)) },
                    placeholder = { Text("e.g. 199", color = Color(0xFF444444)) },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedBorderColor = gold.copy(alpha = 0.6f),
                        unfocusedBorderColor = Color(0xFF2A2A2A),
                        cursorColor = gold
                    ),
                    keyboardActions = KeyboardActions(onSearch = { doSearch() }),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search)
                )
            }

            // ── Variant image picker ──────────────────────────────────────────────
            if (isLoadingVariants) {
                Spacer(Modifier.height(12.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                    CircularProgressIndicator(modifier = Modifier.size(20.dp), color = gold, strokeWidth = 2.dp)
                    Spacer(Modifier.width(10.dp))
                    Text("Finding variants…", color = Color(0xFF666666), fontSize = 12.sp)
                }
            } else if (variants.isNotEmpty()) {
                Spacer(Modifier.height(12.dp))
                Text(
                    "${variants.size} VARIANT${if (variants.size != 1) "S" else ""} — TAP TO PRICE",
                    color = Color(0xFF555555), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp
                )
                Spacer(Modifier.height(8.dp))
                // Use Row + horizontalScroll instead of LazyRow — LazyRow inside
                // verticalScroll causes a fatal layout crash in Compose.
                Row(
                    modifier = Modifier.horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    variants.forEachIndexed { idx, v ->
                        val isSelected = idx == selectedIdx
                        val enPrice = v.weighted
                        val adjPrice = enPrice * langFactor
                        val showDiscount = activeLang != "EN" && enPrice > 0
                        val cacheKey = listOfNotNull(
                            v.name.lowercase().trim(),
                            v.set_name.ifBlank { null }?.lowercase(),
                            v.number.ifBlank { null }
                        ).joinToString("::")
                        val prefetched = state.marketSearchCache[cacheKey]
                        val displayPrice = if (prefetched != null) prefetched.weighted_avg * langFactor else adjPrice
                        val hasPrefetch = prefetched != null
                        Surface(
                            onClick = { onSelectVariant(idx) },
                            color = if (isSelected) Color(0xFF1A2210) else Color(0xFF1A1A1A),
                            shape = RoundedCornerShape(10.dp),
                            border = if (isSelected) BorderStroke(1.5.dp, gold) else BorderStroke(1.dp, Color(0xFF2A2A2A)),
                            modifier = Modifier.width(138.dp)
                        ) {
                            Column(Modifier.padding(horizontal = 8.dp, vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                                if (v.image_small.isNotBlank()) {
                                    AsyncImage(
                                        model = v.image_small,
                                        contentDescription = v.name,
                                        modifier = Modifier.height(108.dp).fillMaxWidth(),
                                        contentScale = androidx.compose.ui.layout.ContentScale.Fit
                                    )
                                } else {
                                    Box(Modifier.height(108.dp).fillMaxWidth().background(Color(0xFF2A2A2A), RoundedCornerShape(6.dp))) {
                                        Text("?", color = Color(0xFF555555), modifier = Modifier.align(Alignment.Center), fontSize = 22.sp)
                                    }
                                }
                                Spacer(Modifier.height(6.dp))
                                if (displayPrice > 0) {
                                    Text(
                                        "$${String.format(java.util.Locale.US, "%.2f", displayPrice)}",
                                        color = if (isSelected) gold else Color.White,
                                        fontWeight = FontWeight.Bold,
                                        fontSize = 14.sp
                                    )
                                    if (showDiscount) {
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Text(
                                                "$${String.format(java.util.Locale.US, "%.2f", if (prefetched != null) prefetched.weighted_avg else enPrice)}",
                                                color = Color(0xFF666666),
                                                fontSize = 9.sp,
                                                textDecoration = androidx.compose.ui.text.style.TextDecoration.LineThrough
                                            )
                                            Spacer(Modifier.width(3.dp))
                                            Text(
                                                "−${langDiscount[activeLang] ?: 0}%",
                                                color = Color(0xFFF59E0B),
                                                fontSize = 8.sp,
                                                fontWeight = FontWeight.Bold
                                            )
                                        }
                                    }
                                    if (hasPrefetch) {
                                        Spacer(Modifier.height(1.dp))
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Icon(Icons.Default.FlashOn, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(7.dp))
                                            Spacer(Modifier.width(2.dp))
                                            Text("READY", color = Color(0xFF4ADE80), fontSize = 7.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                } else {
                                    Text("—", color = Color(0xFF555555), fontSize = 14.sp)
                                }
                                Spacer(Modifier.height(2.dp))
                                Text(
                                    v.set_name.take(18).ifBlank { "Unknown Set" },
                                    color = Color(0xFFAAAAAA), fontSize = 9.sp, maxLines = 1,
                                    overflow = TextOverflow.Ellipsis, textAlign = androidx.compose.ui.text.style.TextAlign.Center
                                )
                                if (v.number.isNotBlank()) {
                                    Text(
                                        "#${v.number}${if (v.rarity.isNotBlank()) " · ${v.rarity.take(8)}" else ""}",
                                        color = Color(0xFF666666), fontSize = 8.sp, maxLines = 1,
                                        overflow = TextOverflow.Ellipsis, textAlign = androidx.compose.ui.text.style.TextAlign.Center
                                    )
                                }
                            }
                        }
                    }
                }
            }

            Spacer(Modifier.height(12.dp))
            Button(
                onClick = { doSearch() },
                enabled = query.isNotBlank() && !isSearching,
                modifier = Modifier.fillMaxWidth().height(48.dp),
                colors = ButtonDefaults.buttonColors(containerColor = gold, contentColor = Color.Black)
            ) {
                if (isSearching) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), color = Color.Black, strokeWidth = 2.dp)
                    Spacer(Modifier.width(8.dp))
                    Text("Searching…", fontWeight = FontWeight.Bold)
                } else {
                    Icon(Icons.Default.Search, null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(6.dp))
                    Text(if (activeLang != "EN") "Search EN · show $activeLang price (−${langDiscount[activeLang] ?: 0}%)" else "Search", fontWeight = FontWeight.Bold)
                }
            }

            // ── Inline error ──────────────────────────────────────────────────────
            if (searchError.isNotBlank() && result == null && !isSearching) {
                Spacer(Modifier.height(10.dp))
                Surface(Modifier.fillMaxWidth(), color = Color(0xFF2A1010), shape = RoundedCornerShape(8.dp)) {
                    Row(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.ErrorOutline, null, tint = Color(0xFFFF6B6B), modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(8.dp))
                        Text(searchError, color = Color(0xFFFF6B6B), fontSize = 12.sp)
                    }
                }
            }

            // ── Result ────────────────────────────────────────────────────────────
            if (result != null) {
                val selVariant = if (selectedIdx in variants.indices) variants[selectedIdx] else null
                Spacer(Modifier.height(20.dp))
                HorizontalDivider(color = Color(0xFF1E1E1E))
                Spacer(Modifier.height(16.dp))

                // Language discount note
                if (activeLang != "EN") {
                    val disc = langDiscount[activeLang] ?: 0
                    Surface(Modifier.fillMaxWidth(), color = Color(0xFF1A1500), shape = RoundedCornerShape(6.dp)) {
                        Row(Modifier.padding(horizontal = 10.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                            val flag = mapOf("JP" to "🇯🇵", "KR" to "🇰🇷", "CN" to "🇨🇳")[activeLang] ?: ""
                            Column {
                                Text("$flag $activeLang pricing · −$disc% applied to EN market price", color = Color(0xFFF59E0B), fontSize = 10.sp)
                                Text("EN base: $${String.format(java.util.Locale.US, "%.2f", result.weighted_avg)}", color = Color(0xFF888888), fontSize = 9.sp)
                            }
                        }
                    }
                    Spacer(Modifier.height(12.dp))
                }

                // ── Card detail with image + set info + nav arrows ──
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
                    if (selVariant != null && selVariant.image_small.isNotBlank()) {
                        Surface(
                            color = Color(0xFF1A1A1A),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.width(100.dp)
                        ) {
                            AsyncImage(
                                model = selVariant.image_small,
                                contentDescription = selVariant.name,
                                modifier = Modifier.height(140.dp).fillMaxWidth().padding(4.dp),
                                contentScale = androidx.compose.ui.layout.ContentScale.Fit
                            )
                        }
                        Spacer(Modifier.width(14.dp))
                    }
                    Column(Modifier.weight(1f)) {
                        Text(result.name.ifBlank { query }, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                        if (selVariant != null) {
                            Spacer(Modifier.height(4.dp))
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Folder, null, tint = Color(0xFF888888), modifier = Modifier.size(12.dp))
                                Spacer(Modifier.width(4.dp))
                                Text(selVariant.set_name.ifBlank { "Unknown Set" }, color = Color(0xFFBBBBBB), fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            }
                            if (selVariant.number.isNotBlank()) {
                                Spacer(Modifier.height(2.dp))
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("#${selVariant.number}", color = gold, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    if (selVariant.rarity.isNotBlank()) {
                                        Spacer(Modifier.width(8.dp))
                                        Surface(color = Color(0xFF2A2A2A), shape = RoundedCornerShape(4.dp)) {
                                            Text(selVariant.rarity, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp), color = Color(0xFFCCCCCC), fontSize = 10.sp)
                                        }
                                    }
                                }
                            }
                            if (selVariant.set_id.isNotBlank()) {
                                Spacer(Modifier.height(2.dp))
                                Text("Set: ${selVariant.set_id.uppercase()}", color = Color(0xFF555555), fontSize = 9.sp)
                            }
                            if (selVariant.artist.isNotBlank()) {
                                Text("Art: ${selVariant.artist}", color = Color(0xFF555555), fontSize = 9.sp)
                            }
                        }
                        Spacer(Modifier.height(6.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("${result.total_samples} sample${if (result.total_samples != 1) "s" else ""}", color = Color(0xFF666666), fontSize = 10.sp)
                            Spacer(Modifier.width(8.dp))
                            val confColor = when (result.confidence.uppercase()) {
                                "HIGH" -> Color(0xFF4ADE80); "MEDIUM" -> Color(0xFFF59E0B); else -> Color(0xFF888888)
                            }
                            Surface(color = confColor.copy(alpha = 0.12f), shape = RoundedCornerShape(4.dp)) {
                                Text(result.confidence.uppercase(), modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp), color = confColor, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                            }
                            if (result.from_cache) {
                                Spacer(Modifier.width(6.dp))
                                Surface(color = Color(0xFF1A2A1A), shape = RoundedCornerShape(4.dp)) {
                                    Row(Modifier.padding(horizontal = 6.dp, vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                                        Icon(Icons.Default.FlashOn, null, tint = Color(0xFF4ADE80), modifier = Modifier.size(8.dp))
                                        Spacer(Modifier.width(2.dp))
                                        Text("CACHED", color = Color(0xFF4ADE80), fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                    }
                                }
                            }
                        }
                    }
                }

                // ── Prev / Next variant navigation ──
                if (variants.size > 1) {
                    Spacer(Modifier.height(10.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                        IconButton(
                            onClick = { if (selectedIdx > 0) onSelectVariant(selectedIdx - 1) },
                            enabled = selectedIdx > 0,
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(Icons.Default.ChevronLeft, null, tint = if (selectedIdx > 0) gold else Color(0xFF333333), modifier = Modifier.size(24.dp))
                        }
                        Text(
                            "${if (selectedIdx >= 0) selectedIdx + 1 else "—"} / ${variants.size} printings",
                            color = Color(0xFF888888), fontSize = 11.sp, fontWeight = FontWeight.Bold
                        )
                        IconButton(
                            onClick = { if (selectedIdx < variants.size - 1) onSelectVariant(selectedIdx + 1) },
                            enabled = selectedIdx < variants.size - 1,
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(Icons.Default.ChevronRight, null, tint = if (selectedIdx < variants.size - 1) gold else Color(0xFF333333), modifier = Modifier.size(24.dp))
                        }
                    }
                }

                Spacer(Modifier.height(12.dp))

                // ── Per-variant price breakdown (normal, holofoil, reverse holo, etc.) ──
                if (selVariant != null && selVariant.tcg_prices.isNotEmpty()) {
                    Text("VARIANT PRICES (TCGPlayer)", color = Color(0xFF444444), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                    Spacer(Modifier.height(6.dp))
                    selVariant.tcg_prices.forEach { tp ->
                        val adjMkt = tp.market * langFactor
                        Row(
                            Modifier.fillMaxWidth().padding(vertical = 2.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                val typeColor = when {
                                    tp.type.contains("holo", ignoreCase = true) -> Color(0xFFF59E0B)
                                    tp.type.contains("reverse", ignoreCase = true) -> Color(0xFF818CF8)
                                    tp.type.contains("1st", ignoreCase = true) -> Color(0xFFE879F9)
                                    else -> Color(0xFF888888)
                                }
                                Surface(color = typeColor.copy(alpha = 0.12f), shape = RoundedCornerShape(3.dp)) {
                                    Text(
                                        tp.type.replace("Holofoil", "Holo").replace("reverseHolofoil", "Rev Holo").replace("1stEditionHolofoil", "1st Holo").replace("1stEditionNormal", "1st Ed").replace("normal", "Normal"),
                                        modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                                        color = typeColor,
                                        fontSize = 10.sp,
                                        fontWeight = FontWeight.Bold
                                    )
                                }
                            }
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("$${String.format(java.util.Locale.US, "%.2f", adjMkt)}", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                if (tp.low != null && tp.high != null) {
                                    Spacer(Modifier.width(6.dp))
                                    Text("(${String.format(java.util.Locale.US, "%.2f", tp.low * langFactor)} – ${String.format(java.util.Locale.US, "%.2f", tp.high * langFactor)})", color = Color(0xFF555555), fontSize = 9.sp)
                                }
                            }
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                }

                // Price grid — all prices adjusted by selected language factor
                val displayAvg   = result.weighted_avg * langFactor
                val displayBuy   = result.buy_price   * langFactor
                val displayTrade = result.trade_value  * langFactor
                Row(Modifier.fillMaxWidth()) {
                    Surface(Modifier.weight(1f), color = Color(0xFF1A1A1A), shape = RoundedCornerShape(8.dp)) {
                        Column(Modifier.padding(12.dp)) {
                            Text("MARKET AVG${if (activeLang != "EN") " ($activeLang)" else ""}", color = Color(0xFF888888), fontSize = 9.sp)
                            Spacer(Modifier.height(4.dp))
                            Text("$${String.format(java.util.Locale.US, "%.2f", displayAvg)}", color = gold, fontWeight = FontWeight.Bold, fontSize = 17.sp)
                        }
                    }
                    Spacer(Modifier.width(8.dp))
                    val badgeColor = when (result.price_badge.uppercase()) {
                        "FAIR" -> Color(0xFF4ADE80); "HIGH" -> Color(0xFFFF5555); else -> Color(0xFFF59E0B)
                    }
                    Surface(Modifier.width(64.dp), color = badgeColor.copy(alpha = 0.12f), shape = RoundedCornerShape(8.dp)) {
                        Column(Modifier.padding(12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("BADGE", color = Color(0xFF888888), fontSize = 8.sp)
                            Spacer(Modifier.height(4.dp))
                            Text(result.price_badge.uppercase(), color = badgeColor, fontWeight = FontWeight.Bold, fontSize = 11.sp)
                        }
                    }
                }
                Spacer(Modifier.height(8.dp))
                Row(Modifier.fillMaxWidth()) {
                    Surface(Modifier.weight(1f), color = Color(0xFF1A1A1A), shape = RoundedCornerShape(8.dp)) {
                        Column(Modifier.padding(12.dp)) {
                            Text("BUY (CASH)", color = Color(0xFF888888), fontSize = 9.sp)
                            Spacer(Modifier.height(4.dp))
                            Text("$${String.format(java.util.Locale.US, "%.2f", displayBuy)}", color = Color(0xFF60A5FA), fontWeight = FontWeight.Bold, fontSize = 15.sp)
                        }
                    }
                    Spacer(Modifier.width(8.dp))
                    Surface(Modifier.weight(1f), color = Color(0xFF1A1A1A), shape = RoundedCornerShape(8.dp)) {
                        Column(Modifier.padding(12.dp)) {
                            Text("BUY (CREDIT)", color = Color(0xFF888888), fontSize = 9.sp)
                            Spacer(Modifier.height(4.dp))
                            Text("$${String.format(java.util.Locale.US, "%.2f", displayTrade)}", color = Color(0xFFD97706), fontWeight = FontWeight.Bold, fontSize = 15.sp)
                        }
                    }
                }

                // Source breakdown
                Spacer(Modifier.height(14.dp))
                Text("SOURCE BREAKDOWN", color = Color(0xFF444444), fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                Spacer(Modifier.height(6.dp))
                listOf(
                    Triple("TCGPlayer", result.sources.pokemontcg, Color(0xFF6366F1)),
                    Triple("eBay Last Sold", result.sources.ebay, Color(0xFFE53935)),
                    Triple("Local History", result.sources.local, gold)
                ).forEach { (label, src, color) ->
                    if (src.count > 0) {
                        Column {
                            Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                                Surface(color = color.copy(alpha = 0.12f), shape = RoundedCornerShape(3.dp)) {
                                    Text(label, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp), color = color, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                                }
                                Spacer(Modifier.width(8.dp))
                                Text("$${String.format(java.util.Locale.US, "%.2f", src.avg)} avg", color = Color(0xFFBBBBBB), fontSize = 11.sp)
                                Spacer(Modifier.width(4.dp))
                                Text("· ${src.count} sale${if (src.count != 1) "s" else ""}", color = Color(0xFF666666), fontSize = 11.sp)
                            }
                            if (label.contains("eBay") && src.prices.isNotEmpty()) {
                                Row(Modifier.padding(start = 12.dp, bottom = 4.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                    src.prices.forEachIndexed { idx, p ->
                                        Surface(color = Color(0xFFE53935).copy(alpha = 0.1f), shape = RoundedCornerShape(4.dp)) {
                                            Text("#${idx + 1}: $${String.format("%.2f", p)}", color = Color(0xFFE53935), fontSize = 9.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Spacer(Modifier.height(10.dp))
                val clipboardManager = androidx.compose.ui.platform.LocalClipboardManager.current
                var copiedPrice by remember { mutableStateOf(false) }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(
                        onClick = {
                            clipboardManager.setText(androidx.compose.ui.text.AnnotatedString(String.format(java.util.Locale.US, "%.2f", displayAvg)))
                            copiedPrice = true
                        },
                        modifier = Modifier.weight(1f).height(38.dp),
                        shape = RoundedCornerShape(8.dp),
                        border = BorderStroke(1.dp, if (copiedPrice) Color(0xFF4ADE80) else Color(0xFF333333)),
                        contentPadding = PaddingValues(0.dp)
                    ) {
                        Icon(if (copiedPrice) Icons.Default.Check else Icons.Default.Share, null, tint = if (copiedPrice) Color(0xFF4ADE80) else Color(0xFF888888), modifier = Modifier.size(14.dp))
                        Spacer(Modifier.width(6.dp))
                        Text(if (copiedPrice) "Copied!" else "Copy Avg", color = if (copiedPrice) Color(0xFF4ADE80) else Color(0xFF888888), fontSize = 11.sp)
                    }
                    OutlinedButton(
                        onClick = {
                            clipboardManager.setText(androidx.compose.ui.text.AnnotatedString(String.format(java.util.Locale.US, "%.2f", displayBuy)))
                        },
                        modifier = Modifier.weight(1f).height(38.dp),
                        shape = RoundedCornerShape(8.dp),
                        border = BorderStroke(1.dp, Color(0xFF333333)),
                        contentPadding = PaddingValues(0.dp)
                    ) {
                        Icon(Icons.Default.Share, null, tint = Color(0xFF888888), modifier = Modifier.size(14.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Copy Buy", color = Color(0xFF888888), fontSize = 11.sp)
                    }
                    OutlinedButton(
                        onClick = { query = ""; onClear() },
                        modifier = Modifier.weight(1f).height(38.dp),
                        shape = RoundedCornerShape(8.dp),
                        border = BorderStroke(1.dp, Color(0xFF333333)),
                        contentPadding = PaddingValues(0.dp)
                    ) {
                        Icon(Icons.Default.Refresh, null, tint = Color(0xFF888888), modifier = Modifier.size(14.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("New Search", color = Color(0xFF888888), fontSize = 11.sp)
                    }
                }

                val insight = result.ai_insight
                if (!insight.isNullOrBlank()) {
                    Spacer(Modifier.height(14.dp))
                    Surface(Modifier.fillMaxWidth(), color = Color(0xFF0E1E0E), shape = RoundedCornerShape(8.dp)) {
                        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.Top) {
                            Text("✦", color = Color(0xFF4ADE80), fontSize = 12.sp)
                            Spacer(Modifier.width(8.dp))
                            Text(insight, color = Color(0xFFAAAAAA), fontSize = 11.sp, lineHeight = 16.sp)
                        }
                    }
                }
            } else if (!isSearching && hasSearched && !isLoadingVariants) {
                Spacer(Modifier.height(20.dp))
                Text("No result found for \"${query.ifBlank { state.marketSearchQuery }}\"", color = Color(0xFF666666), fontSize = 12.sp, modifier = Modifier.align(Alignment.CenterHorizontally))
            }
        }
    }
}

@Composable
fun HaggleAssistantDialog(
    result: HaggleResult?,
    isLoading: Boolean,
    itemName: String,
    onDismiss: () -> Unit
) {
    val flashAnim = rememberInfiniteTransition(label = "haggleFlash")
    val flashAlpha by flashAnim.animateFloat(
        initialValue = 0.3f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(600, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "hfa"
    )
    val pulseScale by flashAnim.animateFloat(
        initialValue = 1f, targetValue = 1.08f,
        animationSpec = infiniteRepeatable(tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "hps"
    )

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.7f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(380.dp).clickable(enabled = false, onClick = {}),
            color = Color(0xFF1A1A2E),
            shape = RoundedCornerShape(20.dp),
            border = BorderStroke(2.dp, Color(0xFF7C4DFF).copy(alpha = flashAlpha))
        ) {
            Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.Psychology, null, tint = Color(0xFF7C4DFF), modifier = Modifier.size(28.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("HAGGLE ASSISTANT", color = Color(0xFF7C4DFF), fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 1.sp)
                }
                Spacer(Modifier.height(8.dp))
                Text(itemName, color = Color.White.copy(alpha = 0.7f), fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Spacer(Modifier.height(16.dp))

                if (isLoading) {
                    CircularProgressIndicator(color = Color(0xFF7C4DFF), modifier = Modifier.size(40.dp))
                    Spacer(Modifier.height(8.dp))
                    Text("Analyzing market data...", color = Color(0xFF888888), fontSize = 12.sp)
                } else if (result != null) {
                    val confidenceColor = when (result.confidence) {
                        "HIGH" -> Color(0xFF4ADE80)
                        "MEDIUM" -> Color(0xFFFFD700)
                        else -> Color(0xFFFF6B6B)
                    }

                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("FLOOR PRICE", color = Color(0xFF888888), fontSize = 9.sp, letterSpacing = 1.sp)
                            Spacer(Modifier.height(4.dp))
                            Text(
                                "$${String.format("%.2f", result.floor_price)}",
                                color = Color(0xFF4ADE80),
                                fontWeight = FontWeight.Black,
                                fontSize = 28.sp,
                                modifier = Modifier.graphicsLayer(scaleX = pulseScale, scaleY = pulseScale)
                            )
                        }
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("WALK AWAY", color = Color(0xFF888888), fontSize = 9.sp, letterSpacing = 1.sp)
                            Spacer(Modifier.height(4.dp))
                            Text(
                                "$${String.format("%.2f", result.walk_away)}",
                                color = Color(0xFFFF6B6B),
                                fontWeight = FontWeight.Black,
                                fontSize = 28.sp
                            )
                        }
                    }
                    Spacer(Modifier.height(16.dp))

                    Surface(Modifier.fillMaxWidth(), color = Color(0xFF0D0D1A), shape = RoundedCornerShape(10.dp)) {
                        Column(Modifier.padding(12.dp)) {
                            Text(result.reasoning, color = Color(0xFFCCCCCC), fontSize = 12.sp, lineHeight = 18.sp)
                            if (result.counter_script.isNotBlank()) {
                                Spacer(Modifier.height(8.dp))
                                Surface(Modifier.fillMaxWidth(), color = Color(0xFF1A0D2A), shape = RoundedCornerShape(6.dp)) {
                                    Row(Modifier.padding(8.dp)) {
                                        Text("💬", fontSize = 12.sp)
                                        Spacer(Modifier.width(6.dp))
                                        Text("\"${result.counter_script}\"", color = Color(0xFFCE93D8), fontSize = 11.sp, lineHeight = 16.sp)
                                    }
                                }
                            }
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                    Surface(color = confidenceColor.copy(alpha = 0.15f), shape = RoundedCornerShape(6.dp)) {
                        Text(
                            "Confidence: ${result.confidence}",
                            color = confidenceColor,
                            fontSize = 10.sp,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
                        )
                    }
                }
                Spacer(Modifier.height(16.dp))
                OutlinedButton(
                    onClick = onDismiss,
                    shape = RoundedCornerShape(10.dp),
                    border = BorderStroke(1.dp, Color(0xFF333333))
                ) {
                    Text("Close", color = Color(0xFF888888))
                }
            }
        }
    }
}

@Composable
fun BundleDealBanner(
    bundleDeal: BundleResult?,
    isLoading: Boolean,
    onRequest: () -> Unit,
    onDismiss: () -> Unit
) {
    val flashAnim = rememberInfiniteTransition(label = "bundleFlash")
    val borderGlow by flashAnim.animateFloat(
        initialValue = 0.2f, targetValue = 0.9f,
        animationSpec = infiniteRepeatable(tween(700, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "bg"
    )
    val savingsScale by flashAnim.animateFloat(
        initialValue = 1f, targetValue = 1.15f,
        animationSpec = infiniteRepeatable(tween(500, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "ss"
    )
    val shimmerOffset by flashAnim.animateFloat(
        initialValue = -1f, targetValue = 2f,
        animationSpec = infiniteRepeatable(tween(1500, easing = LinearEasing), RepeatMode.Restart),
        label = "shimmer"
    )

    if (bundleDeal != null && bundleDeal.has_deal) {
        Surface(
            Modifier.fillMaxWidth(),
            color = Color(0xFF0D1A0D),
            shape = RoundedCornerShape(12.dp),
            border = BorderStroke(1.5.dp, Color(0xFF4ADE80).copy(alpha = borderGlow))
        ) {
            Box(Modifier.fillMaxWidth()) {
                Box(
                    Modifier
                        .fillMaxWidth()
                        .height(60.dp)
                        .background(
                            Brush.horizontalGradient(
                                colors = listOf(Color.Transparent, Color(0xFF4ADE80).copy(alpha = 0.08f), Color.Transparent),
                                startX = shimmerOffset * 300f,
                                endX = (shimmerOffset + 0.5f) * 300f
                            )
                        )
                )
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("🎁", fontSize = 16.sp)
                    Spacer(Modifier.width(8.dp))
                    Column(Modifier.weight(1f)) {
                        Text(bundleDeal.pitch, color = Color(0xFF4ADE80), fontSize = 12.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(
                            "Save $${String.format("%.2f", bundleDeal.savings)} (${bundleDeal.discount_pct}% off)",
                            color = Color(0xFFFFD700),
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Black,
                            modifier = Modifier.graphicsLayer(scaleX = savingsScale, scaleY = savingsScale)
                        )
                    }
                    IconButton(onClick = onDismiss, modifier = Modifier.size(24.dp)) {
                        Icon(Icons.Default.Close, null, tint = Color(0xFF444444), modifier = Modifier.size(14.dp))
                    }
                }
            }
        }
    } else if (isLoading) {
        Surface(
            Modifier.fillMaxWidth(),
            color = Color(0xFF0D1A0D),
            shape = RoundedCornerShape(12.dp)
        ) {
            Row(
                Modifier.padding(horizontal = 14.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp), color = Color(0xFF4ADE80), strokeWidth = 1.5.dp)
                Spacer(Modifier.width(10.dp))
                Text("Calculating bundle deal...", color = Color(0xFF4ADE80).copy(alpha = 0.6f), fontSize = 12.sp)
            }
        }
    } else {
        OutlinedButton(
            onClick = onRequest,
            modifier = Modifier.fillMaxWidth().height(36.dp),
            shape = RoundedCornerShape(10.dp),
            border = BorderStroke(1.dp, Color(0xFF4ADE80).copy(alpha = 0.3f)),
            contentPadding = PaddingValues(0.dp)
        ) {
            Text("🎁", fontSize = 12.sp)
            Spacer(Modifier.width(6.dp))
            Text("Get Bundle Deal", color = Color(0xFF4ADE80), fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
fun VoiceInputDialog(
    onResult: (String) -> Unit,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    var isListening by remember { mutableStateOf(false) }
    var recognizedText by remember { mutableStateOf("") }
    var hasPermission by remember { mutableStateOf(
        android.content.pm.PackageManager.PERMISSION_GRANTED ==
            androidx.core.content.ContextCompat.checkSelfPermission(context, android.Manifest.permission.RECORD_AUDIO)
    ) }

    val permLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        hasPermission = granted
    }

    val speechListener = remember {
        object : android.speech.RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) { isListening = true }
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() { isListening = false }
            override fun onError(error: Int) {
                isListening = false
                if (recognizedText.isBlank()) recognizedText = "Could not understand. Try again."
            }
            override fun onResults(results: Bundle?) {
                val matches = results?.getStringArrayList(android.speech.SpeechRecognizer.RESULTS_RECOGNITION)
                val best = matches?.firstOrNull() ?: ""
                recognizedText = best
                isListening = false
                if (best.isNotBlank()) onResult(best)
            }
            override fun onPartialResults(partialResults: Bundle?) {
                val partial = partialResults?.getStringArrayList(android.speech.SpeechRecognizer.RESULTS_RECOGNITION)
                recognizedText = partial?.firstOrNull() ?: recognizedText
            }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        }
    }

    val speechRecognizer = remember { android.speech.SpeechRecognizer.createSpeechRecognizer(context) }

    DisposableEffect(Unit) {
        speechRecognizer.setRecognitionListener(speechListener)
        onDispose { speechRecognizer.destroy() }
    }

    fun startListening() {
        if (!hasPermission) {
            permLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
            return
        }
        recognizedText = ""
        val intent = android.content.Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(android.speech.RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(android.speech.RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        }
        speechRecognizer.startListening(intent)
    }

    LaunchedEffect(Unit) { startListening() }

    val pulseAnim = rememberInfiniteTransition(label = "voicePulse")
    val micScale by pulseAnim.animateFloat(
        initialValue = 1f,
        targetValue = if (isListening) 1.3f else 1f,
        animationSpec = infiniteRepeatable(tween(400), RepeatMode.Reverse),
        label = "ms"
    )
    val micGlow by pulseAnim.animateFloat(
        initialValue = 0.3f,
        targetValue = if (isListening) 1f else 0.3f,
        animationSpec = infiniteRepeatable(tween(600), RepeatMode.Reverse),
        label = "mg"
    )

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.8f)).clickable(onClick = {
            speechRecognizer.cancel()
            onDismiss()
        }),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(320.dp).clickable(enabled = false, onClick = {}),
            color = Color(0xFF1A1A2E),
            shape = RoundedCornerShape(24.dp),
            border = BorderStroke(2.dp, Color(0xFF7C4DFF).copy(alpha = micGlow))
        ) {
            Column(Modifier.padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text("VOICE SEARCH", color = Color(0xFF7C4DFF), fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 2.sp)
                Spacer(Modifier.height(20.dp))

                Box(
                    modifier = Modifier
                        .size(80.dp)
                        .graphicsLayer(scaleX = micScale, scaleY = micScale)
                        .clip(CircleShape)
                        .background(
                            if (isListening) Color(0xFF7C4DFF).copy(alpha = 0.25f)
                            else Color(0xFF2A2A3E)
                        )
                        .clickable { startListening() },
                    contentAlignment = Alignment.Center
                ) {
                    Icon(Icons.Default.Mic, null, tint = if (isListening) Color(0xFF7C4DFF) else Color(0xFF555555), modifier = Modifier.size(36.dp))
                }
                Spacer(Modifier.height(16.dp))

                Text(
                    if (isListening) "Listening..."
                    else if (recognizedText.isNotBlank()) "\"$recognizedText\""
                    else "Tap mic to start",
                    color = if (isListening) Color(0xFF7C4DFF) else Color.White,
                    fontSize = 14.sp,
                    textAlign = TextAlign.Center,
                    fontWeight = if (isListening) FontWeight.Bold else FontWeight.Normal
                )
                Spacer(Modifier.height(16.dp))
                Text("Say a card name like \"Charizard ex\"", color = Color(0xFF555555), fontSize = 11.sp)
                Spacer(Modifier.height(16.dp))
                OutlinedButton(
                    onClick = { speechRecognizer.cancel(); onDismiss() },
                    shape = RoundedCornerShape(10.dp),
                    border = BorderStroke(1.dp, Color(0xFF333333))
                ) {
                    Text("Cancel", color = Color(0xFF888888))
                }
            }
        }
    }
}

@Composable
fun CounterfeitScannerDialog(
    result: CounterfeitResult?,
    isLoading: Boolean,
    onCapture: (imageB64: String, cardName: String) -> Unit,
    onDismiss: () -> Unit
) {
    var cardNameInput by remember { mutableStateOf("") }
    var hasCaptured by remember { mutableStateOf(false) }
    val context = LocalContext.current

    val cameraLauncher = rememberLauncherForActivityResult(ActivityResultContracts.TakePicturePreview()) { bitmap ->
        if (bitmap != null) {
            hasCaptured = true
            val stream = java.io.ByteArrayOutputStream()
            bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 85, stream)
            val b64 = android.util.Base64.encodeToString(stream.toByteArray(), android.util.Base64.NO_WRAP)
            onCapture(b64, cardNameInput.trim())
        }
    }

    val flashAnim = rememberInfiniteTransition(label = "cfFlash")
    val borderPulse by flashAnim.animateFloat(
        initialValue = 0.3f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "cfbp"
    )
    val scanLineY by flashAnim.animateFloat(
        initialValue = 0f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(2000, easing = LinearEasing), RepeatMode.Restart),
        label = "sly"
    )

    val verdictColor = when (result?.verdict) {
        "LIKELY_AUTHENTIC" -> Color(0xFF4ADE80)
        "SUSPICIOUS" -> Color(0xFFFFD700)
        "LIKELY_FAKE" -> Color(0xFFFF4444)
        else -> Color(0xFF888888)
    }

    val verdictIcon = when (result?.verdict) {
        "LIKELY_AUTHENTIC" -> Icons.Default.CheckCircle
        "SUSPICIOUS" -> Icons.Default.Warning
        "LIKELY_FAKE" -> Icons.Default.Dangerous
        else -> Icons.Default.HelpOutline
    }

    val verdictLabel = when (result?.verdict) {
        "LIKELY_AUTHENTIC" -> "LIKELY AUTHENTIC"
        "SUSPICIOUS" -> "SUSPICIOUS"
        "LIKELY_FAKE" -> "LIKELY FAKE"
        "ERROR" -> "ERROR"
        else -> "UNKNOWN"
    }

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.8f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(400.dp).clickable(enabled = false, onClick = {}),
            color = Color(0xFF0D1A0D),
            shape = RoundedCornerShape(20.dp),
            border = BorderStroke(2.dp, Color(0xFF00E676).copy(alpha = borderPulse))
        ) {
            Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.VerifiedUser, null, tint = Color(0xFF00E676), modifier = Modifier.size(28.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("COUNTERFEIT CHECK", color = Color(0xFF00E676), fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 1.sp)
                }
                Spacer(Modifier.height(16.dp))

                if (result == null && !isLoading) {
                    OutlinedTextField(
                        value = cardNameInput,
                        onValueChange = { cardNameInput = it },
                        placeholder = { Text("Card name (optional)", color = Color(0xFF555555), fontSize = 13.sp) },
                        modifier = Modifier.fillMaxWidth().height(48.dp),
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = Color(0xFF00E676),
                            unfocusedBorderColor = Color(0xFF333333),
                            focusedContainerColor = Color(0xFF111111),
                            unfocusedContainerColor = Color(0xFF111111)
                        )
                    )
                    Spacer(Modifier.height(16.dp))
                    Button(
                        onClick = { cameraLauncher.launch(null) },
                        modifier = Modifier.fillMaxWidth().height(52.dp),
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF00E676), contentColor = Color.Black)
                    ) {
                        Icon(Icons.Default.CameraAlt, null, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(8.dp))
                        Text("SCAN CARD", fontWeight = FontWeight.Black, fontSize = 14.sp)
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("Take a clear photo of the front of the card", color = Color(0xFF555555), fontSize = 11.sp)
                } else if (isLoading) {
                    Spacer(Modifier.height(20.dp))
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(120.dp)
                            .clip(RoundedCornerShape(12.dp))
                            .background(Color(0xFF111111)),
                        contentAlignment = Alignment.Center
                    ) {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(2.dp)
                                .graphicsLayer(translationY = scanLineY * 120f - 60f)
                                .background(Color(0xFF00E676).copy(alpha = 0.6f))
                        )
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            CircularProgressIndicator(color = Color(0xFF00E676), modifier = Modifier.size(32.dp), strokeWidth = 2.dp)
                            Spacer(Modifier.height(8.dp))
                            Text("Analyzing card authenticity...", color = Color(0xFF00E676).copy(alpha = 0.7f), fontSize = 12.sp)
                        }
                    }
                } else if (result != null) {
                    Spacer(Modifier.height(8.dp))
                    Surface(
                        Modifier.fillMaxWidth(),
                        color = verdictColor.copy(alpha = 0.1f),
                        shape = RoundedCornerShape(14.dp),
                        border = BorderStroke(1.5.dp, verdictColor.copy(alpha = 0.4f))
                    ) {
                        Column(Modifier.padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                            Icon(verdictIcon, null, tint = verdictColor, modifier = Modifier.size(40.dp))
                            Spacer(Modifier.height(8.dp))
                            Text(verdictLabel, color = verdictColor, fontWeight = FontWeight.Black, fontSize = 20.sp, letterSpacing = 1.sp)
                            Spacer(Modifier.height(4.dp))
                            Text(
                                "Confidence: ${(result.confidence * 100).toInt()}%",
                                color = verdictColor.copy(alpha = 0.7f),
                                fontSize = 12.sp
                            )
                        }
                    }
                    Spacer(Modifier.height(12.dp))

                    if (result.findings.isNotEmpty()) {
                        Text("FINDINGS", color = Color(0xFF888888), fontSize = 10.sp, letterSpacing = 1.sp, modifier = Modifier.align(Alignment.Start))
                        Spacer(Modifier.height(6.dp))
                        result.findings.forEach { finding ->
                            Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                                Text("•", color = Color(0xFF4ADE80), fontSize = 11.sp)
                                Spacer(Modifier.width(6.dp))
                                Text(finding, color = Color(0xFFCCCCCC), fontSize = 11.sp, lineHeight = 16.sp)
                            }
                        }
                    }

                    if (result.red_flags.isNotEmpty()) {
                        Spacer(Modifier.height(10.dp))
                        Text("RED FLAGS", color = Color(0xFFFF4444), fontSize = 10.sp, letterSpacing = 1.sp, modifier = Modifier.align(Alignment.Start))
                        Spacer(Modifier.height(6.dp))
                        result.red_flags.forEach { flag ->
                            Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                                Text("⚠️", fontSize = 10.sp)
                                Spacer(Modifier.width(6.dp))
                                Text(flag, color = Color(0xFFFF6B6B), fontSize = 11.sp, lineHeight = 16.sp)
                            }
                        }
                    }

                    if (result.recommendation.isNotBlank()) {
                        Spacer(Modifier.height(12.dp))
                        Surface(Modifier.fillMaxWidth(), color = Color(0xFF111111), shape = RoundedCornerShape(8.dp)) {
                            Row(Modifier.padding(10.dp)) {
                                Text("💡", fontSize = 12.sp)
                                Spacer(Modifier.width(6.dp))
                                Text(result.recommendation, color = Color(0xFFAAAADD), fontSize = 11.sp, lineHeight = 16.sp)
                            }
                        }
                    }
                    Spacer(Modifier.height(12.dp))
                    OutlinedButton(
                        onClick = { cameraLauncher.launch(null) },
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(10.dp),
                        border = BorderStroke(1.dp, Color(0xFF00E676).copy(alpha = 0.4f))
                    ) {
                        Icon(Icons.Default.CameraAlt, null, tint = Color(0xFF00E676), modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(6.dp))
                        Text("Scan Another", color = Color(0xFF00E676), fontSize = 12.sp)
                    }
                }

                Spacer(Modifier.height(12.dp))
                OutlinedButton(
                    onClick = onDismiss,
                    shape = RoundedCornerShape(10.dp),
                    border = BorderStroke(1.dp, Color(0xFF333333))
                ) {
                    Text("Close", color = Color(0xFF888888))
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VariantPickerSheet(
    product: ProductEntity,
    variants: List<CardVariant>,
    isLoading: Boolean,
    onSelect: (CardVariant) -> Unit,
    onQuickAdd: () -> Unit,
    onDismiss: () -> Unit
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = Color(0xFF121212),
        tonalElevation = 0.dp,
        dragHandle = null,
        windowInsets = WindowInsets(0)
    ) {
        Column(Modifier.fillMaxWidth().fillMaxHeight(0.85f).padding(horizontal = 16.dp, vertical = 16.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("SELECT PRINTING", color = Gold, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 2.sp)
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { onQuickAdd() }) {
                    Text("Quick Add", color = Color(0xFF888888), fontSize = 12.sp)
                }
                IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                    Icon(Icons.Default.Close, null, tint = Color(0xFF666666), modifier = Modifier.size(18.dp))
                }
            }
            Text(product.name, color = Color.White, fontSize = 16.sp, fontWeight = FontWeight.Bold)
            Text("Long-press adds at stored price. Pick a printing below for live market price.", color = Color(0xFF666666), fontSize = 11.sp)
            Spacer(Modifier.height(12.dp))

            if (isLoading) {
                Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        CircularProgressIndicator(color = Gold, modifier = Modifier.size(32.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.height(12.dp))
                        Text("Loading printings…", color = Color(0xFF666666), fontSize = 12.sp)
                    }
                }
            } else if (variants.isEmpty()) {
                Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Default.SearchOff, null, tint = Color(0xFF333333), modifier = Modifier.size(48.dp))
                        Spacer(Modifier.height(8.dp))
                        Text("No printings found", color = Color(0xFF555555), fontSize = 13.sp)
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = { onQuickAdd() }, colors = ButtonDefaults.buttonColors(containerColor = Gold)) {
                            Text("Add at stored price ($${String.format("%.2f", product.price)})", color = Color.Black, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            } else {
                LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    items(variants.size) { idx ->
                        val v = variants[idx]
                        val rawTypes = listOf("normal", "1st edition normal", "unlimited", "unlimited normal")
                        val rawPrice = v.tcg_prices.filter { it.type.lowercase() in rawTypes }.maxByOrNull { it.market }?.market
                        val displayPrice = rawPrice ?: v.tcg_prices.minByOrNull { it.market }?.market ?: v.weighted
                        Surface(
                            color = Color(0xFF1A1A1A),
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.fillMaxWidth().clickable { onSelect(v) }
                        ) {
                            Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
                                if (v.image_small.isNotBlank()) {
                                    AsyncImage(
                                        model = v.image_small,
                                        contentDescription = null,
                                        modifier = Modifier.width(50.dp).height(70.dp).clip(RoundedCornerShape(6.dp))
                                    )
                                    Spacer(Modifier.width(10.dp))
                                }
                                Column(Modifier.weight(1f)) {
                                    Text(v.name, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                    Text(v.set_name, color = Color(0xFF888888), fontSize = 11.sp, maxLines = 1)
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        if (v.number.isNotBlank()) {
                                            Text("#${v.number}", color = Color(0xFF666666), fontSize = 10.sp)
                                            Spacer(Modifier.width(8.dp))
                                        }
                                        if (v.rarity.isNotBlank()) {
                                            Text(v.rarity, color = Color(0xFF7C4DFF).copy(alpha = 0.7f), fontSize = 10.sp)
                                        }
                                    }
                                    if (v.tcg_prices.isNotEmpty()) {
                                        Spacer(Modifier.height(2.dp))
                                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                            v.tcg_prices.take(3).forEach { tp ->
                                                val isRaw = tp.type.lowercase() in rawTypes
                                                Text(
                                                    "${tp.type}: $${String.format("%.2f", tp.market)}",
                                                    color = if (isRaw) Color(0xFF4ADE80) else Color(0xFF666666),
                                                    fontSize = 9.sp,
                                                    fontWeight = if (isRaw) FontWeight.Bold else FontWeight.Normal
                                                )
                                            }
                                        }
                                    }
                                }
                                Spacer(Modifier.width(8.dp))
                                Column(horizontalAlignment = Alignment.End) {
                                    Text("$${String.format("%.2f", displayPrice)}", color = Gold, fontSize = 15.sp, fontWeight = FontWeight.Black)
                                    Text("raw", color = Color(0xFF4ADE80), fontSize = 9.sp)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// ── OCR Card Scanner ──────────────────────────────────────────────────────────

// Returns (badgeColor, badgeLabel) for confidence display per TABLET_API.md
private fun recognizerBadge(method: String, score: Double): Pair<androidx.compose.ui.graphics.Color, String> = when {
    method == "ocr_number"             -> androidx.compose.ui.graphics.Color(0xFF4CAF50) to "EXACT"
    method == "phash" && score >= 0.85 -> androidx.compose.ui.graphics.Color(0xFF4CAF50) to "HIGH"
    method == "phash" && score >= 0.70 -> androidx.compose.ui.graphics.Color(0xFFFFC107) to "MED"
    method == "phash"                  -> androidx.compose.ui.graphics.Color(0xFFFF9800) to "LOW"
    else                               -> androidx.compose.ui.graphics.Color(0xFFF44336) to "?"
}

@Composable
fun OcrScanDialog(
    state: POSViewState,
    onCapture: (imageB64: String) -> Unit,
    onCaptureVisual: (imageB64: String) -> Unit,
    onCaptureSmart: (imageB64: String) -> Unit,
    onFetchWorldPrice: (query: String) -> Unit,
    onFetchPriceV2: (request: PriceV2Request) -> Unit,
    onLogPick: (candidates: List<LogPickCandidate>, pickedIndex: Int, action: String) -> Unit,
    onAddToCart: (name: String, price: Double) -> Unit,
    onDismiss: () -> Unit
) {
    var selectedTab by remember { mutableStateOf(0) } // 0=Smart 1=OCR 2=Visual
    var smartCondition by remember { mutableStateOf("NM") }

    val smartColor = Color(0xFF00BFA5)
    val ocrColor   = Color(0xFF29B6F6)
    val visColor   = Color(0xFF9C6FF7)
    val activeColor = when (selectedTab) { 0 -> smartColor; 1 -> ocrColor; else -> visColor }

    fun bitmapToB64(bitmap: android.graphics.Bitmap): String {
        val stream = java.io.ByteArrayOutputStream()
        bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 85, stream)
        return android.util.Base64.encodeToString(stream.toByteArray(), android.util.Base64.NO_WRAP)
    }

    val cameraLauncherSmart = rememberLauncherForActivityResult(ActivityResultContracts.TakePicturePreview()) { bitmap ->
        if (bitmap != null) onCaptureSmart(bitmapToB64(bitmap))
    }
    val cameraLauncherOcr = rememberLauncherForActivityResult(ActivityResultContracts.TakePicturePreview()) { bitmap ->
        if (bitmap != null) onCapture(bitmapToB64(bitmap))
    }
    val cameraLauncherVisual = rememberLauncherForActivityResult(ActivityResultContracts.TakePicturePreview()) { bitmap ->
        if (bitmap != null) onCaptureVisual(bitmapToB64(bitmap))
    }

    val isLoading = when (selectedTab) { 0 -> state.isSmartScanLoading; 1 -> state.isOcrScanLoading; else -> state.isVisualScanLoading }

    // Auto-pick logic (per TABLET_API.md): ocr_number → 1.5 s, phash ≥ 0.85 → 2 s
    val topSmartResult = state.smartScanResult?.results?.firstOrNull()
    val autoPickDelayMs: Long? = remember(topSmartResult?.card_id) {
        when {
            topSmartResult == null -> null
            topSmartResult.method == "ocr_number"             -> 1500L
            topSmartResult.method == "phash" && topSmartResult.score >= 0.85 -> 2000L
            else -> null
        }
    }
    var autoPickElapsed by remember(topSmartResult?.card_id) { mutableStateOf(0L) }
    var autoPickDone   by remember(topSmartResult?.card_id) { mutableStateOf(false) }

    LaunchedEffect(topSmartResult?.card_id, autoPickDelayMs) {
        if (topSmartResult == null || autoPickDelayMs == null || autoPickDone) return@LaunchedEffect
        val start = System.currentTimeMillis()
        while (System.currentTimeMillis() - start < autoPickDelayMs) {
            autoPickElapsed = System.currentTimeMillis() - start
            delay(50)
        }
        autoPickDone = true
        val name = listOfNotNull(topSmartResult.name, topSmartResult.card_number?.let { "#$it" }, topSmartResult.set_code).joinToString(" ")
        val price = state.priceV2Result?.median_usd ?: 0.0
        val cands = state.smartScanResult!!.results.map { m -> LogPickCandidate(m.source, m.card_id, m.method, m.score, m.name) }
        onLogPick(cands, 0, "accepted")
        onAddToCart(name, price)
    }

    val pulseAnim = rememberInfiniteTransition(label = "ocrPulse")
    val borderAlpha by pulseAnim.animateFloat(
        initialValue = 0.3f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "ocrBorder"
    )

    Box(
        Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.87f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            Modifier.width(500.dp).heightIn(max = 720.dp).clickable(enabled = false, onClick = {}),
            color = Color(0xFF08131E),
            shape = RoundedCornerShape(20.dp),
            border = BorderStroke(2.dp, activeColor.copy(alpha = if (isLoading) borderAlpha else 0.4f))
        ) {
            Column(Modifier.padding(24.dp).verticalScroll(rememberScrollState())) {

                // Header
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Icon(Icons.Default.DocumentScanner, null, tint = activeColor, modifier = Modifier.size(22.dp))
                        Column {
                            Text("SCAN ANY CARD", color = activeColor, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 1.sp)
                            Text("EN • KR • CHS • JP • Pocket • MTG • Lorcana • One Piece", color = Color(0xFF3A6A80), fontSize = 9.sp)
                        }
                    }
                    IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                        Icon(Icons.Default.Close, null, tint = Color(0xFF555555), modifier = Modifier.size(18.dp))
                    }
                }

                Spacer(Modifier.height(12.dp))

                // Tab switcher — Smart | OCR | Visual
                Row(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(Color(0xFF0D1A25)),
                    horizontalArrangement = Arrangement.spacedBy(0.dp)
                ) {
                    listOf(
                        0 to Triple("SMART",  Icons.Default.AutoAwesome, smartColor),
                        1 to Triple("OCR",    Icons.Default.TextFields,  ocrColor),
                        2 to Triple("VISUAL", Icons.Default.Visibility,  visColor)
                    ).forEach { (idx, data) ->
                        val (label, icon, color) = data
                        val isSelected = selectedTab == idx
                        Box(
                            Modifier.weight(1f)
                                .background(if (isSelected) color.copy(alpha = 0.15f) else Color.Transparent)
                                .clickable { selectedTab = idx }
                                .padding(vertical = 10.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                                Icon(icon, null, tint = if (isSelected) color else Color(0xFF3A5A70), modifier = Modifier.size(13.dp))
                                Text(label, color = if (isSelected) color else Color(0xFF3A5A70), fontSize = 11.sp, fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal)
                            }
                        }
                    }
                }

                Spacer(Modifier.height(14.dp))

                // ── SMART TAB (primary scanner — OCR+phash recognizer) ─────────────────────
                if (selectedTab == 0) when {
                    state.isSmartScanLoading -> {
                        Box(Modifier.fillMaxWidth().height(160.dp), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                                CircularProgressIndicator(color = smartColor, modifier = Modifier.size(48.dp), strokeWidth = 3.dp)
                                Text("Recognizing...", color = smartColor, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                                Text("OCR number + perceptual hash", color = Color(0xFF1A4040), fontSize = 11.sp)
                            }
                        }
                    }

                    state.smartScanResult != null -> {
                        val result = state.smartScanResult
                        if (result.results.isEmpty()) {
                            Box(Modifier.fillMaxWidth().padding(vertical = 28.dp), contentAlignment = Alignment.Center) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Icon(Icons.Default.SearchOff, null, tint = Color(0xFF444444), modifier = Modifier.size(44.dp))
                                    Text("No match found", color = Color(0xFF666666), fontSize = 13.sp)
                                    Text("Try better lighting or straight-on angle", color = Color(0xFF444444), fontSize = 11.sp)
                                }
                            }
                        } else {
                            Text(
                                "${result.count} CANDIDATE${if (result.count != 1) "S" else ""}",
                                color = smartColor.copy(alpha = 0.7f), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp
                            )
                            Spacer(Modifier.height(8.dp))
                            result.results.forEachIndexed { idx, match ->
                                RecognizerMatchCard(
                                    match = match,
                                    isTop = idx == 0,
                                    priceV2 = if (idx == 0) state.priceV2Result else null,
                                    isV2Loading = idx == 0 && state.isV2PriceFetching,
                                    autoPickDelayMs = if (idx == 0) autoPickDelayMs else null,
                                    autoPickElapsed = if (idx == 0) autoPickElapsed else 0L,
                                    autoPickDone = if (idx == 0) autoPickDone else false,
                                    selectedCondition = if (idx == 0) smartCondition else "NM",
                                    onConditionChange = if (idx == 0) { cond ->
                                        smartCondition = cond
                                        onFetchPriceV2(PriceV2Request(
                                            query = listOfNotNull(match.name, match.card_number, match.set_code).joinToString(" "),
                                            card_id = match.card_id,
                                            condition = cond
                                        ))
                                    } else null,
                                    onAddToCart = { price ->
                                        val name = listOfNotNull(match.name, match.card_number?.let { "#$it" }, match.set_code).joinToString(" ")
                                        val cands = result.results.map { m -> LogPickCandidate(m.source, m.card_id, m.method, m.score, m.name) }
                                        val action = if (idx == 0) "accepted" else "overridden"
                                        onLogPick(cands, idx, action)
                                        onAddToCart(name, price)
                                    },
                                    onCancelAutoPick = { autoPickDone = true }
                                )
                                if (idx < result.results.size - 1) Spacer(Modifier.height(8.dp))
                            }
                        }
                        Spacer(Modifier.height(14.dp))
                        OutlinedButton(
                            onClick = { cameraLauncherSmart.launch(null) },
                            modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(10.dp),
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = smartColor),
                            border = BorderStroke(1.dp, smartColor.copy(alpha = 0.4f))
                        ) {
                            Icon(Icons.Default.CameraAlt, null, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("SCAN AGAIN", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        }
                    }

                    else -> {
                        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
                            Spacer(Modifier.height(8.dp))
                            Surface(color = smartColor.copy(alpha = 0.07f), shape = RoundedCornerShape(10.dp)) {
                                Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        Icon(Icons.Default.AutoAwesome, null, tint = smartColor, modifier = Modifier.size(15.dp))
                                        Text("SMART SCAN — PRIMARY", color = smartColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    }
                                    Text("Reads the card number via OCR, then confirms with perceptual hash. High confidence results add to cart automatically.", color = Color(0xFF2A5050), fontSize = 10.sp, lineHeight = 15.sp)
                                }
                            }
                            Spacer(Modifier.height(16.dp))
                            Button(
                                onClick = { cameraLauncherSmart.launch(null) },
                                modifier = Modifier.fillMaxWidth().height(62.dp),
                                shape = RoundedCornerShape(14.dp),
                                colors = ButtonDefaults.buttonColors(containerColor = smartColor, contentColor = Color.Black)
                            ) {
                                Icon(Icons.Default.AutoAwesome, null, modifier = Modifier.size(26.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("SMART SCAN", fontWeight = FontWeight.Black, fontSize = 16.sp)
                            }
                            Spacer(Modifier.height(10.dp))
                            Text("Auto-picks card in 1.5 s (exact OCR) or 2 s (high-confidence visual)", color = Color(0xFF2A5050), fontSize = 10.sp, textAlign = TextAlign.Center)
                            Spacer(Modifier.height(8.dp))
                        }
                    }
                }

                // ── OCR TAB (tab index 1) ──────────────────────────────────────────────────
                if (selectedTab == 1) when {
                    state.isOcrScanLoading -> {
                        Box(Modifier.fillMaxWidth().height(160.dp), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                                CircularProgressIndicator(color = ocrColor, modifier = Modifier.size(48.dp), strokeWidth = 3.dp)
                                Text("Reading card...", color = ocrColor, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                                Text("OCR + language detection in progress", color = Color(0xFF3A5A70), fontSize = 11.sp)
                            }
                        }
                    }

                    state.ocrScanResult != null -> {
                        val result = state.ocrScanResult

                        // Detected text / scripts strip
                        if (!result.raw_text.isNullOrBlank() || result.script_tags.isNotEmpty()) {
                            Surface(color = Color(0xFF0D1A25), shape = RoundedCornerShape(8.dp)) {
                                Row(
                                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Icon(Icons.Default.TextFields, null, tint = Color(0xFF3A5A70), modifier = Modifier.size(14.dp))
                                    Text(
                                        result.candidate ?: result.raw_text?.take(40) ?: "",
                                        color = Color(0xFF889EAA),
                                        fontSize = 12.sp,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                        modifier = Modifier.weight(1f)
                                    )
                                    result.script_tags.forEach { tag ->
                                        val tagColor = when (tag) {
                                            "hangul" -> Color(0xFF00BCD4)
                                            "cjk" -> Color(0xFFEF5350)
                                            "kana" -> Color.White
                                            else -> Color(0xFF888888)
                                        }
                                        Surface(color = tagColor.copy(alpha = 0.12f), shape = RoundedCornerShape(4.dp)) {
                                            Text(tag.uppercase(), color = tagColor, fontSize = 9.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                        }
                                    }
                                }
                            }
                            Spacer(Modifier.height(12.dp))
                        }

                        if (result.matches.isEmpty()) {
                            Box(Modifier.fillMaxWidth().padding(vertical = 28.dp), contentAlignment = Alignment.Center) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Icon(Icons.Default.SearchOff, null, tint = Color(0xFF444444), modifier = Modifier.size(44.dp))
                                    Text("No matches found", color = Color(0xFF666666), fontSize = 13.sp)
                                    Text("Try better lighting or a cleaner angle", color = Color(0xFF444444), fontSize = 11.sp)
                                }
                            }
                        } else {
                            Text(
                                "${result.matches.size} MATCH${if (result.matches.size != 1) "ES" else ""}  FOUND",
                                color = Color(0xFF3A6A80), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp
                            )
                            Spacer(Modifier.height(8.dp))
                            result.matches.forEachIndexed { idx, match ->
                                OcrMatchCard(
                                    match = match,
                                    worldPrices = if (idx == 0) state.worldPrices else null,
                                    isWorldPriceFetching = idx == 0 && state.isWorldPriceFetching,
                                    onFetchWorldPrice = { onFetchWorldPrice(match.name) },
                                    onAddToCart = { price ->
                                        val fullName = buildString {
                                            append(match.name)
                                            if (!match.set_name.isNullOrBlank()) append(" — ${match.set_name}")
                                            if (!match.number.isNullOrBlank()) append(" #${match.number}")
                                        }
                                        onAddToCart(fullName, price)
                                    }
                                )
                                if (idx < result.matches.size - 1) Spacer(Modifier.height(8.dp))
                            }
                        }

                        Spacer(Modifier.height(14.dp))
                        OutlinedButton(
                            onClick = { cameraLauncherOcr.launch(null) },
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp),
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = ocrColor),
                            border = BorderStroke(1.dp, ocrColor.copy(alpha = 0.4f))
                        ) {
                            Icon(Icons.Default.CameraAlt, null, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("SCAN AGAIN", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        }
                    }

                    else -> {
                        // OCR Idle state
                        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
                            Spacer(Modifier.height(8.dp))
                            Row(
                                Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(5.dp, Alignment.CenterHorizontally)
                            ) {
                                listOf(
                                    "EN" to Gold,
                                    "KR" to Color(0xFF00BCD4),
                                    "CHS" to Color(0xFFEF5350),
                                    "JP" to Color.White,
                                    "MTG" to Color(0xFFCD7F32),
                                    "OP" to Color(0xFFE91E63),
                                    "LOR" to Color(0xFF9C27B0)
                                ).forEach { (label, color) ->
                                    Surface(color = color.copy(alpha = 0.1f), shape = RoundedCornerShape(4.dp)) {
                                        Text(label, color = color, fontSize = 10.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 7.dp, vertical = 4.dp))
                                    }
                                }
                            }
                            Spacer(Modifier.height(20.dp))
                            Button(
                                onClick = { cameraLauncherOcr.launch(null) },
                                modifier = Modifier.fillMaxWidth().height(62.dp),
                                shape = RoundedCornerShape(14.dp),
                                colors = ButtonDefaults.buttonColors(containerColor = ocrColor, contentColor = Color.Black)
                            ) {
                                Icon(Icons.Default.CameraAlt, null, modifier = Modifier.size(26.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("SCAN CARD (OCR)", fontWeight = FontWeight.Black, fontSize = 16.sp)
                            }
                            Spacer(Modifier.height(14.dp))
                            Text(
                                "Point the camera at any card in any language. The Pi identifies it and fetches market prices automatically.",
                                color = Color(0xFF3A5A70),
                                fontSize = 11.sp,
                                textAlign = TextAlign.Center,
                                lineHeight = 16.sp
                            )
                            Spacer(Modifier.height(8.dp))
                        }
                    }
                }

                // ── VISUAL MATCH TAB (tab index 2) ────────────────────────────────────────
                if (selectedTab == 2) when {
                    state.isVisualScanLoading -> {
                        Box(Modifier.fillMaxWidth().height(160.dp), contentAlignment = Alignment.Center) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
                                CircularProgressIndicator(color = visColor, modifier = Modifier.size(48.dp), strokeWidth = 3.dp)
                                Text("Matching visually...", color = visColor, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                                Text("CLIP + FAISS image embedding search", color = Color(0xFF3A3060), fontSize = 11.sp)
                            }
                        }
                    }

                    state.visualScanResult != null -> {
                        val result = state.visualScanResult

                        // Model badge
                        if (!result.model.isNullOrBlank()) {
                            Surface(color = visColor.copy(alpha = 0.1f), shape = RoundedCornerShape(6.dp)) {
                                Row(
                                    Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 6.dp),
                                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Icon(Icons.Default.ImageSearch, null, tint = visColor, modifier = Modifier.size(13.dp))
                                    Text(result.model.uppercase(), color = visColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp)
                                    Spacer(Modifier.weight(1f))
                                    Text("visual similarity", color = Color(0xFF553A80), fontSize = 9.sp)
                                }
                            }
                            Spacer(Modifier.height(10.dp))
                        }

                        if (result.matches.isEmpty()) {
                            Box(Modifier.fillMaxWidth().padding(vertical = 28.dp), contentAlignment = Alignment.Center) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Icon(Icons.Default.SearchOff, null, tint = Color(0xFF444444), modifier = Modifier.size(44.dp))
                                    Text("No visual match found", color = Color(0xFF666666), fontSize = 13.sp)
                                    Text("Try a clearer, well-lit shot of the card art", color = Color(0xFF444444), fontSize = 11.sp)
                                }
                            }
                        } else {
                            Text(
                                "${result.matches.size} VISUAL MATCH${if (result.matches.size != 1) "ES" else ""}",
                                color = visColor.copy(alpha = 0.7f), fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp
                            )
                            Spacer(Modifier.height(8.dp))
                            result.matches.forEachIndexed { idx, match ->
                                OcrMatchCard(
                                    match = match,
                                    worldPrices = if (idx == 0) state.worldPrices else null,
                                    isWorldPriceFetching = idx == 0 && state.isWorldPriceFetching,
                                    onFetchWorldPrice = { onFetchWorldPrice(match.name) },
                                    onAddToCart = { price ->
                                        val fullName = buildString {
                                            append(match.name)
                                            if (!match.set_name.isNullOrBlank()) append(" — ${match.set_name}")
                                            if (!match.number.isNullOrBlank()) append(" #${match.number}")
                                        }
                                        onAddToCart(fullName, price)
                                    }
                                )
                                if (idx < result.matches.size - 1) Spacer(Modifier.height(8.dp))
                            }
                        }

                        Spacer(Modifier.height(14.dp))
                        OutlinedButton(
                            onClick = { cameraLauncherVisual.launch(null) },
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(10.dp),
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = visColor),
                            border = BorderStroke(1.dp, visColor.copy(alpha = 0.4f))
                        ) {
                            Icon(Icons.Default.CameraAlt, null, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("SCAN AGAIN", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        }
                    }

                    else -> {
                        // Visual idle state
                        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
                            Spacer(Modifier.height(8.dp))
                            Surface(color = visColor.copy(alpha = 0.07f), shape = RoundedCornerShape(10.dp)) {
                                Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        Icon(Icons.Default.ImageSearch, null, tint = visColor, modifier = Modifier.size(16.dp))
                                        Text("HOW IT WORKS", color = visColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    }
                                    Text("Your Pi runs a CLIP neural network that converts card images into visual embeddings. It then searches a FAISS index of all known cards to find the closest visual match — works even for worn, non-English, or obscure cards.", color = Color(0xFF445566), fontSize = 10.sp, lineHeight = 15.sp)
                                }
                            }
                            Spacer(Modifier.height(16.dp))
                            Button(
                                onClick = { cameraLauncherVisual.launch(null) },
                                modifier = Modifier.fillMaxWidth().height(62.dp),
                                shape = RoundedCornerShape(14.dp),
                                colors = ButtonDefaults.buttonColors(containerColor = visColor, contentColor = Color.Black)
                            ) {
                                Icon(Icons.Default.Visibility, null, modifier = Modifier.size(26.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("VISUAL MATCH", fontWeight = FontWeight.Black, fontSize = 16.sp)
                            }
                            Spacer(Modifier.height(14.dp))
                            Text(
                                "Best for: worn cards, non-English prints, damaged barcodes, or any card where text is unclear.",
                                color = Color(0xFF3A5A70),
                                fontSize = 11.sp,
                                textAlign = TextAlign.Center,
                                lineHeight = 16.sp
                            )
                            Spacer(Modifier.height(8.dp))
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun RecognizerMatchCard(
    match: RecognizerMatch,
    isTop: Boolean,
    priceV2: PriceV2Result?,
    isV2Loading: Boolean,
    autoPickDelayMs: Long?,
    autoPickElapsed: Long,
    autoPickDone: Boolean,
    selectedCondition: String = "NM",
    onConditionChange: ((String) -> Unit)? = null,
    onAddToCart: (price: Double) -> Unit,
    onCancelAutoPick: () -> Unit
) {
    val (badgeColor, badgeLabel) = recognizerBadge(match.method, match.score)
    val isAutoEligible = isTop && autoPickDelayMs != null && !autoPickDone
    val autoProgress = if (isAutoEligible && autoPickDelayMs != null && autoPickDelayMs > 0)
        (autoPickElapsed.toFloat() / autoPickDelayMs.toFloat()).coerceIn(0f, 1f) else 0f

    val displayName = listOfNotNull(
        match.name,
        match.card_number?.let { "#$it" },
        match.set_code?.ifBlank { null }
    ).joinToString(" · ")
    val displaySource = match.source.ifBlank { "tcg" }.uppercase()

    var priceInput by remember(match.card_id) {
        mutableStateOf(priceV2?.median_usd?.let { "%.2f".format(it) } ?: "")
    }
    LaunchedEffect(priceV2?.median_usd) {
        if (priceV2 != null && priceInput.isEmpty()) {
            priceInput = "%.2f".format(priceV2.median_usd)
        }
    }

    Surface(
        color = if (isTop) Color(0xFF0D2020) else Color(0xFF0A1818),
        shape = RoundedCornerShape(10.dp),
        border = if (isTop) BorderStroke(1.5.dp, badgeColor.copy(alpha = 0.5f)) else BorderStroke(1.dp, Color(0xFF1A3A3A))
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (isTop) Icon(Icons.Default.AutoAwesome, null, tint = badgeColor, modifier = Modifier.size(14.dp))
                Text(displayName, color = Color.White, fontSize = 13.sp, fontWeight = if (isTop) FontWeight.Bold else FontWeight.Normal, modifier = Modifier.weight(1f), maxLines = 2)
                Surface(color = badgeColor.copy(alpha = 0.2f), shape = RoundedCornerShape(4.dp)) {
                    Text(
                        "$badgeLabel  ${"%.0f".format(match.score * 100)}%",
                        color = badgeColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                    )
                }
                Text(displaySource, color = Color(0xFF3A7070), fontSize = 9.sp, fontWeight = FontWeight.SemiBold)
            }

            // Auto-pick countdown ring
            if (isAutoEligible) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(contentAlignment = Alignment.Center, modifier = Modifier.size(28.dp)) {
                        CircularProgressIndicator(progress = { autoProgress }, color = Color(0xFF00BFA5), modifier = Modifier.size(28.dp), strokeWidth = 2.5.dp, trackColor = Color(0xFF0D2020))
                        Text(
                            "${((autoPickDelayMs!! - autoPickElapsed) / 1000.0 + 0.99).toInt()}",
                            color = Color(0xFF00BFA5), fontSize = 10.sp, fontWeight = FontWeight.Bold
                        )
                    }
                    Text("Auto-adding to cart…", color = Color(0xFF00BFA5), fontSize = 11.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    TextButton(onClick = onCancelAutoPick, contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)) {
                        Text("CANCEL", color = Color(0xFFEF5350), fontSize = 10.sp, fontWeight = FontWeight.Bold)
                    }
                }
            }

            // Price v2 info row (top result only)
            if (isTop && priceV2 != null) {
                val volColor = if (priceV2.volatile_flag) Color(0xFFFF9800) else Color(0xFF4ADE80)

                // Price + condition + volatile
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Market:", color = Color(0xFF3A7070), fontSize = 10.sp)
                    Text("${"%.2f".format(priceV2.median_usd ?: 0.0)}", color = volColor, fontSize = 13.sp, fontWeight = FontWeight.Bold)
                    priceV2.condition?.takeIf { it.isNotBlank() }?.let {
                        Surface(color = Color(0xFF1A3A3A), shape = RoundedCornerShape(4.dp)) {
                            Text(it.uppercase(), color = Color(0xFF3A7070), fontSize = 9.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                        }
                    }
                    if (priceV2.volatile_flag) {
                        Surface(color = Color(0xFFFF9800).copy(alpha = 0.15f), shape = RoundedCornerShape(4.dp)) {
                            Text("VOLATILE", color = Color(0xFFFF9800), fontSize = 9.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                        }
                    }
                    Spacer(Modifier.weight(1f))
                    if (priceV2.sample_count > 0) {
                        Text("${priceV2.sample_count} listings", color = Color(0xFF3A5050), fontSize = 9.sp)
                    }
                }

                // Source badges row
                if (priceV2.sources_used.isNotEmpty()) {
                    val sourceColors = mapOf(
                        "ebay"      to Color(0xFFE53935),
                        "naver"     to Color(0xFF43A047),
                        "tcgkorea"  to Color(0xFF1E88E5),
                        "snkrdunk"  to Color(0xFF8E24AA),
                        "cardmarket" to Color(0xFFFFB300),
                        "tcgplayer" to Color(0xFF00ACC1)
                    )
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(5.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("Sources:", color = Color(0xFF2A5050), fontSize = 9.sp)
                        priceV2.sources_used.forEach { src ->
                            val srcKey = src.lowercase().replace("-", "").replace("_", "")
                            val c = sourceColors.entries.firstOrNull { srcKey.contains(it.key) }?.value ?: Color(0xFF555555)
                            Surface(color = c.copy(alpha = 0.15f), shape = RoundedCornerShape(3.dp)) {
                                Text(
                                    src.uppercase(),
                                    color = c, fontSize = 8.sp, fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp)
                                )
                            }
                        }
                    }
                }
            }
            if (isTop && isV2Loading) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    CircularProgressIndicator(color = Color(0xFF00BFA5), modifier = Modifier.size(12.dp), strokeWidth = 1.5.dp)
                    Text("Fetching market price…", color = Color(0xFF3A7070), fontSize = 10.sp)
                }
            }

            // Condition picker (top result only, when price callback available)
            if (isTop && onConditionChange != null) {
                val conditions = listOf("NM", "LP", "MP", "HP")
                val condColors = mapOf(
                    "NM" to Color(0xFF4CAF50),
                    "LP" to Color(0xFF8BC34A),
                    "MP" to Color(0xFFFF9800),
                    "HP" to Color(0xFFEF5350)
                )
                Row(
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("Cond:", color = Color(0xFF2A5050), fontSize = 9.sp)
                    conditions.forEach { cond ->
                        val condColor = condColors[cond] ?: Color(0xFF555555)
                        val isSelected = selectedCondition == cond
                        Surface(
                            color = if (isSelected) condColor.copy(alpha = 0.25f) else Color(0xFF0D1E1E),
                            shape = RoundedCornerShape(5.dp),
                            border = BorderStroke(if (isSelected) 1.dp else 0.5.dp, if (isSelected) condColor else Color(0xFF1A3A3A)),
                            modifier = Modifier.clickable { onConditionChange(cond) }
                        ) {
                            Text(
                                cond, color = if (isSelected) condColor else Color(0xFF3A5050),
                                fontSize = 10.sp, fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                            )
                        }
                    }
                }
            }

            // Price input + add button
            if (!isAutoEligible || autoPickDone) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                    OutlinedTextField(
                        value = priceInput,
                        onValueChange = { priceInput = it.filter { c -> c.isDigit() || c == '.' } },
                        label = { Text("Price $", fontSize = 11.sp) },
                        modifier = Modifier.weight(1f).height(52.dp),
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = badgeColor,
                            focusedLabelColor = badgeColor,
                            unfocusedBorderColor = Color(0xFF1A3A3A),
                            unfocusedLabelColor = Color(0xFF3A7070)
                        ),
                        textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 13.sp)
                    )
                    Button(
                        onClick = { onAddToCart(priceInput.toDoubleOrNull() ?: 0.0) },
                        colors = ButtonDefaults.buttonColors(containerColor = badgeColor, contentColor = Color.Black),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.height(52.dp)
                    ) {
                        Icon(Icons.Default.AddShoppingCart, null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("ADD", fontWeight = FontWeight.Bold, fontSize = 12.sp)
                    }
                }
            }
        }
    }
}

@Composable
private fun OcrMatchCard(
    match: OcrCardMatch,
    worldPrices: WorldPriceResult?,
    isWorldPriceFetching: Boolean,
    onFetchWorldPrice: () -> Unit,
    onAddToCart: (Double) -> Unit
) {
    var priceInput by remember(match.name, match.set_name) {
        mutableStateOf(if ((match.price ?: 0.0) > 0.0) "%.2f".format(match.price) else "")
    }

    val langColor = when (match.lang?.lowercase()) {
        "en" -> Gold
        "kr" -> Color(0xFF00BCD4)
        "chs", "cn" -> Color(0xFFEF5350)
        "jp", "jpn" -> Color(0xFFE0E0E0)
        "pocket" -> Color(0xFF5C6BC0)
        "mtg" -> Color(0xFFCD7F32)
        "onepiece", "op" -> Color(0xFFE91E63)
        "lorcana" -> Color(0xFF9C27B0)
        else -> Color(0xFF888888)
    }
    val langLabel = when (match.lang?.lowercase()) {
        "en" -> "EN"; "kr" -> "KR"; "chs", "cn" -> "CHS"; "jp", "jpn" -> "JP"
        "pocket" -> "POCKET"; "mtg" -> "MTG"; "onepiece", "op" -> "ONE PIECE"
        "lorcana" -> "LORCANA"
        else -> match.lang?.uppercase() ?: "?"
    }
    val isAsian = match.lang?.lowercase() in listOf("kr", "chs", "cn", "jp", "jpn", "pocket")
    val price = priceInput.toDoubleOrNull() ?: 0.0

    Surface(
        color = Color(0xFF0E1E2E),
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, langColor.copy(alpha = 0.25f))
    ) {
        Column(Modifier.padding(14.dp)) {

            // Name row + lang badge
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f)) {
                    Text(match.name, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 14.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    val sub = listOfNotNull(
                        match.set_name,
                        match.number?.let { "#$it" },
                        match.rarity
                    ).joinToString("  •  ")
                    if (sub.isNotBlank()) Text(sub, color = Color(0xFF556677), fontSize = 11.sp)
                }
                Spacer(Modifier.width(8.dp))
                Surface(color = langColor.copy(alpha = 0.15f), shape = RoundedCornerShape(5.dp)) {
                    Text(langLabel, color = langColor, fontSize = 10.sp, fontWeight = FontWeight.Black, modifier = Modifier.padding(horizontal = 7.dp, vertical = 3.dp))
                }
            }

            // Confidence bar
            val score = match.score ?: 0.0
            if (score > 0) {
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    LinearProgressIndicator(
                        progress = { score.toFloat().coerceIn(0f, 1f) },
                        modifier = Modifier.weight(1f).height(4.dp).clip(RoundedCornerShape(2.dp)),
                        color = langColor,
                        trackColor = Color(0xFF1A2A3A)
                    )
                    Text("${(score * 100).toInt()}%", color = langColor, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                }
            }

            Spacer(Modifier.height(10.dp))

            // Price + Add to Cart
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = priceInput,
                    onValueChange = { v ->
                        val clean = v.filter { it.isDigit() || it == '.' }
                        if (clean.count { it == '.' } <= 1) priceInput = clean
                    },
                    modifier = Modifier.weight(1f).height(50.dp),
                    singleLine = true,
                    label = { Text("Price (USD)", fontSize = 10.sp) },
                    placeholder = { Text("0.00", fontSize = 12.sp, color = Color(0xFF334455)) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Done),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = langColor,
                        unfocusedBorderColor = Color(0xFF1E3040),
                        focusedLabelColor = langColor,
                        unfocusedLabelColor = Color(0xFF445566),
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        cursorColor = langColor
                    ),
                    shape = RoundedCornerShape(8.dp),
                    leadingIcon = {
                        Text("$", color = Color(0xFF445566), fontSize = 14.sp, modifier = Modifier.padding(start = 10.dp))
                    }
                )
                Button(
                    onClick = { onAddToCart(price) },
                    enabled = price > 0,
                    modifier = Modifier.height(50.dp),
                    shape = RoundedCornerShape(8.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = langColor,
                        contentColor = Color.Black,
                        disabledContainerColor = Color(0xFF1A2A3A),
                        disabledContentColor = Color(0xFF3A4A5A)
                    )
                ) {
                    Icon(Icons.Default.AddShoppingCart, null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("ADD", fontWeight = FontWeight.Black, fontSize = 12.sp)
                }
            }

            // World prices section (auto-fetched for Asian cards, manual for others)
            if (isAsian || worldPrices != null) {
                Spacer(Modifier.height(8.dp))
                when {
                    isWorldPriceFetching -> {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            CircularProgressIndicator(modifier = Modifier.size(13.dp), color = langColor, strokeWidth = 1.5.dp)
                            Text("Fetching world prices...", color = Color(0xFF3A6A80), fontSize = 10.sp)
                        }
                    }
                    worldPrices != null && worldPrices.count > 0 -> {
                        Surface(color = Color(0xFF071218), shape = RoundedCornerShape(8.dp)) {
                            Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                    Row(horizontalArrangement = Arrangement.spacedBy(5.dp), verticalAlignment = Alignment.CenterVertically) {
                                        Icon(Icons.Default.Language, null, tint = langColor, modifier = Modifier.size(12.dp))
                                        Text("WORLD PRICES", color = langColor, fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
                                    }
                                    worldPrices.median_usd?.let { med ->
                                        Text("Median ${"%.2f".format(med)}", color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Bold)
                                    }
                                }
                                HorizontalDivider(color = Color(0xFF0F2030), thickness = 0.5.dp)
                                listOfNotNull(
                                    worldPrices.results.tcgkorea?.firstOrNull()?.let  { Triple("TCGKOREA",   Color(0xFF1E88E5), it) },
                                    worldPrices.results.naver?.firstOrNull()?.let     { Triple("NAVER",      Color(0xFF43A047), it) },
                                    worldPrices.results.snkrdunk?.firstOrNull()?.let  { Triple("SNKRDUNK",   Color(0xFF8E24AA), it) },
                                    worldPrices.results.cardmarket?.firstOrNull()?.let{ Triple("CARDMARKET", Color(0xFFFFB300), it) }
                                ).forEach { (srcName, srcColor, row) ->
                                    Row(
                                        Modifier.fillMaxWidth(),
                                        horizontalArrangement = Arrangement.SpaceBetween,
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        Surface(color = srcColor.copy(alpha = 0.15f), shape = RoundedCornerShape(3.dp)) {
                                            Text(srcName, color = srcColor, fontSize = 8.sp, fontWeight = FontWeight.Bold,
                                                modifier = Modifier.padding(horizontal = 5.dp, vertical = 2.dp))
                                        }
                                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                                            Text("${row.currency} ${"%.0f".format(row.price)}", color = Color(0xFF556677), fontSize = 10.sp)
                                            row.price_usd?.let { usd ->
                                                Text("${"$%.2f".format(usd)}", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                                            }
                                        }
                                    }
                                }
                                worldPrices.median_usd?.let { med ->
                                    if (med > 0) {
                                        TextButton(
                                            onClick = { priceInput = "%.2f".format(med) },
                                            contentPadding = PaddingValues(0.dp),
                                            modifier = Modifier.height(22.dp)
                                        ) {
                                            Icon(Icons.Default.PriceCheck, null, tint = langColor.copy(alpha = 0.7f), modifier = Modifier.size(12.dp))
                                            Spacer(Modifier.width(4.dp))
                                            Text("Use median  ${"$%.2f".format(med)}", color = langColor.copy(alpha = 0.7f), fontSize = 10.sp)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    isAsian -> {
                        TextButton(
                            onClick = onFetchWorldPrice,
                            contentPadding = PaddingValues(0.dp),
                            modifier = Modifier.height(24.dp)
                        ) {
                            Icon(Icons.Default.Language, null, tint = langColor.copy(alpha = 0.6f), modifier = Modifier.size(12.dp))
                            Spacer(Modifier.width(4.dp))
                            Text("Fetch world prices (KR/JP/EU)", color = langColor.copy(alpha = 0.6f), fontSize = 10.sp)
                        }
                    }
                }
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Customer Signing Screen — shown automatically when Trade-In panel opens
// ─────────────────────────────────────────────────────────────────────────────
@Composable
fun CustomerSigningScreen(
    items: List<TradeInItem>,
    disclosure: String,
    decision: String,
    onAccept: (ByteArray?, String) -> Unit,
    onReject: () -> Unit,
    onDecisionComplete: () -> Unit,
    onBackToAdmin: () -> Unit,
    onUpdateDisclosure: (String) -> Unit = {},
    autoTimeoutSeconds: Int = 60
) {
    val cashTotal   = items.sumOf { it.buyOffer }
    val creditTotal = items.sumOf { it.tradeCredit }
    var showDisclosureEdit by remember { mutableStateOf(false) }
    var disclosureDraft   by remember(disclosure) { mutableStateOf(disclosure) }
    // Signature pad state — strokes captured from finger drawing.
    val strokes = remember { mutableStateListOf<List<Offset>>() }
    var currentStroke by remember { mutableStateOf<List<Offset>>(emptyList()) }
    val hasSignature = strokes.isNotEmpty() || currentStroke.size > 1
    var customerName by remember { mutableStateOf("") }
    var canvasSize by remember { mutableStateOf(androidx.compose.ui.geometry.Size.Zero) }
    var secondsLeft by remember { mutableStateOf(autoTimeoutSeconds) }

    val ctx = LocalContext.current
    val haptic = remember(ctx) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (ctx.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            ctx.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
    }
    fun buzz(ms: Long) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                haptic?.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION") haptic?.vibrate(ms)
            }
        } catch (_: Exception) {}
    }
    fun beep(tone: Int) {
        try {
            val tg = ToneGenerator(AudioManager.STREAM_MUSIC, 80)
            tg.startTone(tone, 200)
            android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({ tg.release() }, 250)
        } catch (_: Exception) {}
    }

    // Render the current strokes to a PNG byte array (for printer + record).
    fun captureSignaturePng(): ByteArray? {
        val w = canvasSize.width.toInt().coerceAtLeast(64)
        val h = canvasSize.height.toInt().coerceAtLeast(48)
        if (strokes.isEmpty() && currentStroke.size < 2) return null
        return try {
            val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            val ac = android.graphics.Canvas(bmp)
            ac.drawColor(android.graphics.Color.WHITE)
            val paint = android.graphics.Paint().apply {
                color = android.graphics.Color.BLACK
                strokeWidth = 5f
                style = android.graphics.Paint.Style.STROKE
                strokeCap = android.graphics.Paint.Cap.ROUND
                strokeJoin = android.graphics.Paint.Join.ROUND
                isAntiAlias = true
            }
            (strokes + listOf(currentStroke.toList())).forEach { stroke ->
                if (stroke.size > 1) {
                    val path = android.graphics.Path()
                    path.moveTo(stroke[0].x, stroke[0].y)
                    for (i in 1 until stroke.size - 1) {
                        val midX = (stroke[i].x + stroke[i + 1].x) / 2f
                        val midY = (stroke[i].y + stroke[i + 1].y) / 2f
                        path.quadTo(stroke[i].x, stroke[i].y, midX, midY)
                    }
                    path.lineTo(stroke.last().x, stroke.last().y)
                    ac.drawPath(path, paint)
                }
            }
            val baos = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 100, baos)
            bmp.recycle()
            baos.toByteArray()
        } catch (_: Exception) { null }
    }

    if (showDisclosureEdit) {
        AlertDialog(
            onDismissRequest = { showDisclosureEdit = false },
            containerColor = Color(0xFF1A1A1A),
            title = { Text("Edit Disclosure", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 15.sp) },
            text = {
                OutlinedTextField(
                    value = disclosureDraft,
                    onValueChange = { disclosureDraft = it },
                    label = { Text("Disclosure text", color = Color(0xFF888888), fontSize = 12.sp) },
                    minLines = 4,
                    maxLines = 8,
                    modifier = Modifier.fillMaxWidth(),
                    colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = Gold,
                        unfocusedBorderColor = Color(0xFF333333),
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White
                    )
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    onUpdateDisclosure(disclosureDraft.trim())
                    showDisclosureEdit = false
                }) {
                    Text("Save", color = Gold, fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { showDisclosureEdit = false }) {
                    Text("Cancel", color = Color(0xFF888888))
                }
            }
        )
    }

    LaunchedEffect(decision) {
        if (decision == "accepted") {
            buzz(40); beep(ToneGenerator.TONE_PROP_ACK)
            kotlinx.coroutines.delay(3000L)
            onDecisionComplete()
        } else if (decision == "rejected") {
            buzz(120); beep(ToneGenerator.TONE_PROP_NACK)
            kotlinx.coroutines.delay(3000L)
            onDecisionComplete()
        }
    }

    // 60-second auto-decline countdown — only ticks while still awaiting decision.
    LaunchedEffect(decision) {
        if (decision.isNotEmpty()) return@LaunchedEffect
        secondsLeft = autoTimeoutSeconds
        while (secondsLeft > 0) {
            kotlinx.coroutines.delay(1000L)
            secondsLeft -= 1
        }
        if (decision.isEmpty()) onReject()
    }

    Box(modifier = Modifier.fillMaxSize().background(VaultBlack).navigationBarsPadding().imePadding()) {
        if (decision == "accepted" || decision == "rejected") {
            val isAccepted = decision == "accepted"
            Column(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Box(
                    modifier = Modifier.size(140.dp).clip(CircleShape)
                        .background(if (isAccepted) Color(0xFF1A3A1A) else Color(0xFF3A1A1A)),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        if (isAccepted) Icons.Default.CheckCircle else Icons.Default.Cancel,
                        null,
                        tint = if (isAccepted) Color(0xFF4CAF50) else Color(0xFFE53935),
                        modifier = Modifier.size(80.dp)
                    )
                }
                Spacer(Modifier.height(32.dp))
                Text(
                    if (isAccepted) "OFFER ACCEPTED" else "OFFER DECLINED",
                    fontSize = 36.sp, fontWeight = FontWeight.Black, letterSpacing = 4.sp,
                    color = if (isAccepted) Color(0xFF4CAF50) else Color(0xFFE53935)
                )
                Spacer(Modifier.height(12.dp))
                Text(
                    if (isAccepted) "Thank you! The operator will complete your trade-in."
                    else "No problem — please speak with the operator.",
                    fontSize = 16.sp, color = Color(0xFFAAAAAA),
                    textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                    modifier = Modifier.padding(horizontal = 40.dp)
                )
            }
        } else {
            Column(modifier = Modifier.fillMaxSize()) {
                // ── Header ─────────────────────────────────────────────────────
                Box(
                    modifier = Modifier.fillMaxWidth().background(Color(0xFF111111))
                        .padding(horizontal = 16.dp, vertical = 12.dp)
                ) {
                    TextButton(onClick = onBackToAdmin, modifier = Modifier.align(Alignment.CenterStart)) {
                        Icon(Icons.Default.ArrowBack, null, tint = Color(0xFF777777), modifier = Modifier.size(16.dp))
                        Spacer(Modifier.width(4.dp))
                        Text("Back to Admin", color = Color(0xFF777777), fontSize = 12.sp)
                    }
                    Column(modifier = Modifier.align(Alignment.Center), horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("VAULT", fontSize = 22.sp, fontWeight = FontWeight.Black, color = Gold, letterSpacing = 8.sp)
                        Text("TRADE-IN OFFER", fontSize = 11.sp, color = Color(0xFF888888), letterSpacing = 3.sp)
                    }
                    // 60s auto-decline countdown badge
                    Box(
                        modifier = Modifier.align(Alignment.CenterEnd)
                            .background(
                                if (secondsLeft <= 10) Color(0xFF3A1A1A) else Color(0xFF222222),
                                RoundedCornerShape(8.dp)
                            )
                            .padding(horizontal = 10.dp, vertical = 6.dp)
                    ) {
                        Text(
                            "${secondsLeft}s",
                            color = if (secondsLeft <= 10) Color(0xFFE53935) else Color(0xFFAAAAAA),
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }

                // ── Item list ──────────────────────────────────────────────────
                LazyColumn(
                    modifier = Modifier.weight(1f).padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    if (items.isEmpty()) {
                        item {
                            Box(modifier = Modifier.fillMaxWidth().padding(top = 60.dp), contentAlignment = Alignment.Center) {
                                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                    Icon(Icons.Default.SwapHoriz, null, tint = Color(0xFF444444), modifier = Modifier.size(48.dp))
                                    Spacer(Modifier.height(12.dp))
                                    Text("No items added yet", color = Color(0xFF666666), fontSize = 16.sp)
                                    Text("Scan cards to see your offer", color = Color(0xFF444444), fontSize = 13.sp)
                                }
                            }
                        }
                    } else {
                        item {
                            Row(
                                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp, horizontal = 4.dp),
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Text("CARD", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp, modifier = Modifier.weight(1f))
                                Text("CASH", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp, modifier = Modifier.width(72.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
                                Text("CREDIT", color = Gold, fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp, modifier = Modifier.width(80.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
                            }
                            Divider(color = Color(0xFF333333), thickness = 0.5.dp)
                        }
                        items(items, key = { it.product.qrCode }) { item ->
                            Row(
                                modifier = Modifier.fillMaxWidth()
                                    .background(Color(0xFF111111), RoundedCornerShape(8.dp))
                                    .padding(horizontal = 12.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(item.product.name, color = Color.White, fontSize = 14.sp, modifier = Modifier.weight(1f), maxLines = 2)
                                Text("\$${String.format("%.2f", item.buyOffer)}", color = Color(0xFFFFD700), fontSize = 14.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.width(72.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
                                Text("\$${String.format("%.2f", item.tradeCredit)}", color = Gold, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.width(80.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
                            }
                        }
                    }
                }

                // ── Signature panel: logo + disclosure on top, finger-draw pad below ─────
                Column(
                    modifier = Modifier.fillMaxWidth().background(Color(0xFF111111)).padding(horizontal = 16.dp, vertical = 10.dp)
                ) {
                    // Logo + disclosure header (tap to edit disclosure).
                    Row(
                        modifier = Modifier.fillMaxWidth().clickable { showDisclosureEdit = true }.padding(bottom = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Image(
                            painter = painterResource(id = R.drawable.ic_launcher_dragon),
                            contentDescription = "Store logo",
                            modifier = Modifier.size(32.dp)
                        )
                        Spacer(Modifier.width(10.dp))
                        Text(
                            if (disclosure.isNotBlank()) disclosure
                            else "By signing below the customer agrees to the trade-in terms.",
                            color = Color(0xFFBBBBBB), fontSize = 10.sp, lineHeight = 13.sp,
                            modifier = Modifier.weight(1f)
                        )
                        Icon(Icons.Default.Edit, null, tint = Color(0xFF666666), modifier = Modifier.size(13.dp))
                    }

                    // Customer name + date row (#3)
                    val today = remember { java.text.SimpleDateFormat("MMM d, yyyy", java.util.Locale.US).format(java.util.Date()) }
                    Row(
                        modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        OutlinedTextField(
                            value = customerName,
                            onValueChange = { customerName = it.take(40) },
                            placeholder = { Text("Customer name", color = Color(0xFF666666), fontSize = 12.sp) },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(
                                capitalization = KeyboardCapitalization.Words,
                                imeAction = ImeAction.Done
                            ),
                            textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 13.sp),
                            colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                                focusedBorderColor = Gold,
                                unfocusedBorderColor = Color(0xFF333333),
                                focusedTextColor = Color.White,
                                unfocusedTextColor = Color.White,
                                cursorColor = Gold
                            ),
                            modifier = Modifier.weight(1f).height(48.dp)
                        )
                        Spacer(Modifier.width(10.dp))
                        Column(horizontalAlignment = Alignment.End) {
                            Text("DATE", color = Color(0xFF666666), fontSize = 9.sp, letterSpacing = 1.sp)
                            Text(today, color = Color(0xFFCCCCCC), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        }
                    }

                    // Signature pad — white canvas, finger draws black ink.
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(100.dp)
                            .background(Color.White, RoundedCornerShape(8.dp))
                    ) {
                        Canvas(
                            modifier = Modifier.fillMaxSize()
                                .onSizeChanged { canvasSize = androidx.compose.ui.geometry.Size(it.width.toFloat(), it.height.toFloat()) }
                                .pointerInput(Unit) {
                                    detectDragGestures(
                                        onDragStart = { offset -> currentStroke = listOf(offset) },
                                        onDrag = { change, _ ->
                                            currentStroke = currentStroke + change.position
                                        },
                                        onDragEnd = {
                                            if (currentStroke.size > 1) strokes.add(currentStroke.toList())
                                            currentStroke = emptyList()
                                        },
                                        onDragCancel = {
                                            if (currentStroke.size > 1) strokes.add(currentStroke.toList())
                                            currentStroke = emptyList()
                                        }
                                    )
                                }
                        ) {
                            (strokes + listOf(currentStroke)).forEach { stroke ->
                                if (stroke.size > 1) {
                                    // Bezier smoothing (#7) — quad through midpoints
                                    val path = Path().apply {
                                        moveTo(stroke[0].x, stroke[0].y)
                                        for (i in 1 until stroke.size - 1) {
                                            val midX = (stroke[i].x + stroke[i + 1].x) / 2f
                                            val midY = (stroke[i].y + stroke[i + 1].y) / 2f
                                            quadraticBezierTo(stroke[i].x, stroke[i].y, midX, midY)
                                        }
                                        lineTo(stroke.last().x, stroke.last().y)
                                    }
                                    drawPath(
                                        path = path,
                                        color = Color.Black,
                                        style = Stroke(width = 5f, cap = StrokeCap.Round, join = StrokeJoin.Round)
                                    )
                                }
                            }
                        }
                        if (!hasSignature) {
                            Text(
                                "Sign here with your finger",
                                color = Color(0xFFAAAAAA), fontSize = 14.sp,
                                modifier = Modifier.align(Alignment.Center)
                            )
                        }
                        // Undo (#6) + Clear buttons
                        Row(modifier = Modifier.align(Alignment.TopEnd).padding(2.dp)) {
                            TextButton(onClick = { if (strokes.isNotEmpty()) strokes.removeAt(strokes.lastIndex) }) {
                                Icon(Icons.Default.Undo, null, tint = Color(0xFF666666), modifier = Modifier.size(14.dp))
                                Spacer(Modifier.width(2.dp))
                                Text("Undo", color = Color(0xFF666666), fontSize = 11.sp)
                            }
                            TextButton(onClick = { strokes.clear(); currentStroke = emptyList() }) {
                                Icon(Icons.Default.Refresh, null, tint = Color(0xFF666666), modifier = Modifier.size(14.dp))
                                Spacer(Modifier.width(2.dp))
                                Text("Clear", color = Color(0xFF666666), fontSize = 11.sp)
                            }
                        }
                    }

                    Spacer(Modifier.height(8.dp))

                    // Totals
                    Row(
                        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
                        horizontalArrangement = Arrangement.SpaceEvenly
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("CASH OFFER", color = Color(0xFF888888), fontSize = 10.sp, letterSpacing = 2.sp)
                            Text("\$${String.format("%.2f", cashTotal)}", color = Color(0xFFFFD700), fontSize = 24.sp, fontWeight = FontWeight.Black)
                        }
                        Box(modifier = Modifier.width(1.dp).height(40.dp).background(Color(0xFF333333)))
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text("STORE CREDIT", color = Color(0xFF888888), fontSize = 10.sp, letterSpacing = 2.sp)
                            Text("\$${String.format("%.2f", creditTotal)}", color = Gold, fontSize = 24.sp, fontWeight = FontWeight.Black)
                        }
                    }

                    // ACCEPT / DECLINE — ACCEPT is disabled until customer signs.
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Button(
                            onClick = onReject,
                            modifier = Modifier.weight(1f).height(54.dp),
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF3A1A1A), contentColor = Color(0xFFE53935)),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Icon(Icons.Default.Cancel, null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("DECLINE", fontSize = 16.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
                        }
                        Button(
                            onClick = {
                                val png = captureSignaturePng()
                                onAccept(png, customerName.trim())
                            },
                            enabled = hasSignature,
                            modifier = Modifier.weight(1f).height(54.dp),
                            colors = ButtonDefaults.buttonColors(
                                containerColor = Color(0xFF1A3A1A), contentColor = Color(0xFF4CAF50),
                                disabledContainerColor = Color(0xFF1A1A1A), disabledContentColor = Color(0xFF555555)
                            ),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Icon(Icons.Default.CheckCircle, null, modifier = Modifier.size(18.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(if (hasSignature) "ACCEPT" else "SIGN TO ACCEPT", fontSize = 16.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
                        }
                    }
                }
            }
        }
    }
}
