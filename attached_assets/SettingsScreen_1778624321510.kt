package com.hanryxvault.pos

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import com.zettle.sdk.ZettleSDK
import com.zettle.sdk.feature.cardreader.ui.CardReaderAction
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun SettingsScreen(
    state: POSViewState,
    onDismiss: () -> Unit,
    onToggleDrawer: (Boolean) -> Unit,
    onToggleKickDrawer: (Boolean) -> Unit = {},
    onUpdateQrData: (String) -> Unit,
    onUpdateDisclaimer: (String) -> Unit = {},
    onToggleRegisterMode: () -> Unit = {},
    onSetPrinter: (address: String, name: String) -> Unit = { _, _ -> },
    onSetPrinterMode: (mode: String) -> Unit = {},
    onSetPrinterEthernet: (host: String, port: Int) -> Unit = { _, _ -> },
    onTestPrinter: () -> Unit = {},
    onTestKickDrawer: () -> Unit = {},
    onSetReceiptLayout: (ReceiptLayout) -> Unit = {},
    onToggleReceiptQr: () -> Unit = {},
    onUpdateQuickSalePreset: (index: Int, label: String) -> Unit = { _, _ -> },
    onForceSync: () -> Unit = {},
    onPingPi: () -> Unit = {},
    onSetOpeningFloat: (Double) -> Unit = {},
    onSetTradeBuyPct: (Double) -> Unit = {},
    onSetTradeCreditPct: (Double) -> Unit = {},
    onUpdateCustomerDisplayDisclosure: (String) -> Unit = {}
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var qrInput by remember { mutableStateOf(state.customReceiptQrData) }
    var disclaimerInput by remember { mutableStateOf(state.receiptDisclaimer) }
    var disclosureInput by remember { mutableStateOf(state.customerDisplayDisclosure) }
    // Lifted so SAVE button can flush all at once
    val quickSaleInputs = remember(state.quickSalePresets) {
        state.quickSalePresets.map { it.label }.toMutableList()
    }
    var showPinDialog by remember { mutableStateOf(false) }
    var pinInput by remember { mutableStateOf("") }
    var pinError by remember { mutableStateOf(false) }
    var showPrinterPicker by remember { mutableStateOf(false) }
    var savedBanner by remember { mutableStateOf(false) }
    var piUrlInput by remember { mutableStateOf(PiUrlPreference.get(context).trimEnd('/')) }
    var piUrlSaved by remember { mutableStateOf(false) }
    var vpnUrlInput by remember { mutableStateOf(VpnUrlPreference.get(context).trimEnd('/')) }
    var vpnUrlSaved by remember { mutableStateOf(false) }
    var venmoHandleInput by remember { mutableStateOf(VenmoHandlePreference.get(context)) }
    var venmoHandleSaved by remember { mutableStateOf(false) }
    var cashAppHandleInput by remember { mutableStateOf(CashAppHandlePreference.get(context)) }
    var cashAppHandleSaved by remember { mutableStateOf(false) }
    var taxRateInput by remember { mutableStateOf(TaxRatePreference.get(context).toString()) }
    var feeRateInput by remember { mutableStateOf(TransactionFeePreference.get(context).toString()) }
    var ratesSaved by remember { mutableStateOf(false) }
    var buyPctInput by remember { mutableStateOf("%.0f".format(BuyPricePreference.get(context) * 100)) }
    var creditPctInput by remember { mutableStateOf("%.0f".format(TradeCreditPreference.get(context) * 100)) }
    var tradePctSaved by remember { mutableStateOf(false) }
    var openingFloatInput by remember { mutableStateOf(state.openingFloat.let { if (it > 0.0) "%.2f".format(it) else "" }) }
    var floatSaved by remember { mutableStateOf(false) }
    var showFloatDialog by remember { mutableStateOf(false) }
    var floatDialogInput by remember { mutableStateOf("") }

    val readerSettingsLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.StartActivityForResult()
    ) { /* no result needed for settings */ }

    val pairedPrinters = remember {
        try {
            BluetoothAdapter.getDefaultAdapter()
                ?.bondedDevices
                ?.map { it.name to it.address }
                ?: emptyList()
        } catch (e: Exception) { emptyList() }
    }

    if (showFloatDialog) {
        AlertDialog(
            onDismissRequest = { showFloatDialog = false },
            containerColor = Color(0xFF1A1A1A),
            shape = RoundedCornerShape(16.dp),
            title = {
                Text("Opening Cash Float", color = Gold, fontWeight = FontWeight.Black, fontSize = 16.sp)
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Enter the cash amount in the drawer at start of day.", color = Color.Gray, fontSize = 12.sp)
                    OutlinedTextField(
                        value = floatDialogInput,
                        onValueChange = { v -> floatDialogInput = v.filter { it.isDigit() || it == '.' } },
                        placeholder = { Text("0.00", color = Color(0xFF444444)) },
                        prefix = { Text("$", color = Color.Gray) },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = Gold,
                            unfocusedBorderColor = Color(0xFF444444),
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            cursorColor = Gold
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val amount = floatDialogInput.toDoubleOrNull() ?: 0.0
                        onSetOpeningFloat(amount)
                        openingFloatInput = floatDialogInput
                        floatSaved = true
                        showFloatDialog = false
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Gold),
                    shape = RoundedCornerShape(8.dp)
                ) {
                    Text("SAVE", color = Color.Black, fontWeight = FontWeight.Black)
                }
            },
            dismissButton = {
                TextButton(onClick = { showFloatDialog = false }) {
                    Text("CANCEL", color = Color.Gray)
                }
            }
        )
    }

    if (showPinDialog) {
        AlertDialog(
            onDismissRequest = { showPinDialog = false; pinInput = ""; pinError = false },
            containerColor = VaultSurface,
            title = {
                Text("EXIT REGISTER MODE", color = Gold, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
            },
            text = {
                Column {
                    Text("Enter admin PIN to disable register lock.", color = Color.White, fontSize = 13.sp)
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = pinInput,
                        onValueChange = { if (it.length <= 4) { pinInput = it; pinError = false } },
                        label = { Text("PIN") },
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                        isError = pinError,
                        supportingText = { if (pinError) Text("Incorrect PIN", color = Color.Red) },
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = Gold
                        )
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        if (pinInput == "1234") {
                            showPinDialog = false; pinInput = ""; pinError = false
                            onToggleRegisterMode()
                        } else pinError = true
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Gold)
                ) { Text("UNLOCK", color = VaultBlack, fontWeight = FontWeight.Black) }
            },
            dismissButton = {
                TextButton(onClick = { showPinDialog = false; pinInput = ""; pinError = false }) {
                    Text("CANCEL", color = Color.Gray)
                }
            }
        )
    }

    if (showPrinterPicker) {
        AlertDialog(
            onDismissRequest = { showPrinterPicker = false },
            containerColor = VaultSurface,
            title = { Text("SELECT BLUETOOTH PRINTER", color = Gold, fontWeight = FontWeight.Black, fontSize = 14.sp, letterSpacing = 1.5.sp) },
            text = {
                Column {
                    if (pairedPrinters.isEmpty()) {
                        Text(
                            "No paired Bluetooth devices found.\nPair your printer in Android Settings → Bluetooth first.",
                            color = Color.Gray, fontSize = 13.sp, lineHeight = 20.sp
                        )
                    } else {
                        pairedPrinters.forEach { (name, address) ->
                            val isSelected = state.printerAddress == address
                            Row(
                                Modifier
                                    .fillMaxWidth()
                                    .clip(RoundedCornerShape(8.dp))
                                    .background(if (isSelected) Color(0xFF1A1400) else Color.Transparent)
                                    .clickable {
                                        onSetPrinter(address, name)
                                        showPrinterPicker = false
                                    }
                                    .padding(12.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Icon(
                                    Icons.Default.Print,
                                    null,
                                    tint = if (isSelected) Gold else Color.Gray,
                                    modifier = Modifier.size(20.dp)
                                )
                                Spacer(Modifier.width(12.dp))
                                Column(Modifier.weight(1f)) {
                                    Text(name, color = Color.White, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                                    Text(address, color = Color.Gray, fontSize = 11.sp)
                                }
                                if (isSelected) {
                                    Icon(Icons.Default.CheckCircle, null, tint = Gold, modifier = Modifier.size(18.dp))
                                }
                            }
                        }
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { showPrinterPicker = false }) {
                    Text("CLOSE", color = Color.Gray)
                }
            }
        )
    }

    Surface(modifier = Modifier.fillMaxSize(), color = VaultBlack) {
        Column(Modifier.padding(32.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Default.ArrowBack, null, tint = Gold)
                    }
                    Text(
                        "SYSTEM DIAGNOSTICS",
                        color = Gold, fontSize = 24.sp, fontWeight = FontWeight.Black, letterSpacing = 4.sp
                    )
                }
                AnimatedVisibility(visible = savedBanner, enter = fadeIn(), exit = fadeOut()) {
                    Surface(color = Color(0xFF1B3A1B), shape = RoundedCornerShape(8.dp)) {
                        Row(Modifier.padding(horizontal = 14.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.CheckCircle, null, tint = Color(0xFF4CAF50), modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("SETTINGS SAVED", color = Color(0xFF4CAF50), fontSize = 11.sp, fontWeight = FontWeight.Black, letterSpacing = 1.sp)
                        }
                    }
                }
            }

            Spacer(Modifier.height(32.dp))

            LazyColumn(verticalArrangement = Arrangement.spacedBy(16.dp)) {

                item { SectionHeader("ZETTLE ACCOUNT") }
                item {
                    val zettleLoggedIn = state.sdkLoggedIn
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(
                                    if (zettleLoggedIn) Icons.Default.AccountCircle else Icons.Default.LockOpen,
                                    null,
                                    tint = if (zettleLoggedIn) Color(0xFF4CAF50) else Color(0xFFFF9800),
                                    modifier = Modifier.size(20.dp)
                                )
                                Spacer(Modifier.width(10.dp))
                                Text(
                                    if (zettleLoggedIn) "ZETTLE — LOGGED IN" else "ZETTLE — NOT LOGGED IN",
                                    color = if (zettleLoggedIn) Color(0xFF4CAF50) else Color(0xFFFF9800),
                                    fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp
                                )
                            }
                            Text(
                                if (zettleLoggedIn)
                                    "Your Zettle merchant account is connected. Card payments are ready to use after pairing your reader."
                                else
                                    "You must log into your Zettle merchant account before card payments will work. Tap the button below to sign in.",
                                color = Color.Gray, fontSize = 12.sp, lineHeight = 17.sp
                            )
                            if (!zettleLoggedIn) {
                                Button(
                                    onClick = {
                                        val activity = context as? android.app.Activity
                                        if (activity == null) {
                                            Toast.makeText(context, "Cannot open login screen", Toast.LENGTH_SHORT).show()
                                            return@Button
                                        }
                                        if (ZettleSDK.isInitialized) {
                                            try {
                                                ZettleSDK.instance?.login(activity, MainActivity.ZETTLE_LOGIN_REQUEST)
                                            } catch (e: Exception) {
                                                Toast.makeText(context, "Login error: ${e.message}", Toast.LENGTH_LONG).show()
                                            }
                                        } else {
                                            Toast.makeText(context, "Zettle SDK not initialized", Toast.LENGTH_SHORT).show()
                                        }
                                    },
                                    modifier = Modifier.fillMaxWidth(),
                                    shape = RoundedCornerShape(8.dp),
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1B5E20))
                                ) {
                                    Icon(Icons.Default.Login, null, tint = Color.White, modifier = Modifier.size(18.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("LOG IN TO ZETTLE", fontWeight = FontWeight.Bold, color = Color.White)
                                }
                            } else {
                                OutlinedButton(
                                    onClick = {
                                        if (ZettleSDK.isInitialized) {
                                            ZettleSDK.instance?.logout { result ->
                                                Toast.makeText(context, "Logged out of Zettle", Toast.LENGTH_SHORT).show()
                                            }
                                        }
                                    },
                                    modifier = Modifier.fillMaxWidth(),
                                    shape = RoundedCornerShape(8.dp),
                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF888888))
                                ) {
                                    Text("LOG OUT OF ZETTLE", fontWeight = FontWeight.Bold, color = Color.Gray)
                                }
                            }
                        }
                    }
                }

                item { SectionHeader("CARD READER") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.CreditCard, null, tint = Gold, modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("ZETTLE CARD READER", color = Gold, fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text(
                                "Pair or connect your Zettle Reader before taking card payments. Open the SDK reader settings to scan for and register your device.",
                                color = Color.Gray, fontSize = 12.sp, lineHeight = 17.sp
                            )
                            Button(
                                onClick = {
                                    if (ZettleSDK.isInitialized) {
                                        try {
                                            val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
                                            val btAdapter = btManager?.adapter
                                            if (btAdapter == null || !btAdapter.isEnabled) {
                                                Toast.makeText(context, "Enable Bluetooth first, then open reader settings.", Toast.LENGTH_LONG).show()
                                            } else {
                                                val intent = ZettleIntentHelper.show(CardReaderAction.Settings, context)
                                                readerSettingsLauncher.launch(intent)
                                            }
                                        } catch (e: Exception) {
                                            Toast.makeText(context, "Reader settings error: ${e.message}", Toast.LENGTH_LONG).show()
                                        }
                                    } else {
                                        Toast.makeText(context, "Zettle SDK not initialized", Toast.LENGTH_SHORT).show()
                                    }
                                },
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(8.dp),
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF0A4D82))
                            ) {
                                Icon(Icons.Default.BluetoothSearching, null, tint = Color.White, modifier = Modifier.size(18.dp))
                                Spacer(Modifier.width(8.dp))
                                Text("PAIR / CONNECT READER", fontWeight = FontWeight.Bold, color = Color.White)
                            }
                        }
                    }
                }

                item { SectionHeader("CONNECTION STATUS") }
                item {
                    StatusRow(
                        label = "RASPBERRY PI SERVER",
                        status = state.piStatus,
                        isOnline = state.piStatus == "OK",
                        icon = Icons.Default.Router
                    )
                }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Router, null, tint = Gold, modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("PI SERVER URL", color = Gold, fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            OutlinedTextField(
                                value = piUrlInput,
                                onValueChange = { piUrlInput = it; piUrlSaved = false },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                placeholder = { Text("http://192.168.x.x:8080  or  http://name.duckdns.org:8080", color = Color.Gray, fontSize = 11.sp) },
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Gold,
                                    unfocusedBorderColor = Color.Gray,
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    cursorColor = Gold
                                ),
                                textStyle = androidx.compose.ui.text.TextStyle(fontSize = 13.sp)
                            )
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button(
                                    onClick = {
                                        if (piUrlInput.isNotBlank()) {
                                            PiUrlPreference.set(context, piUrlInput.trim())
                                            piUrlSaved = true
                                        }
                                    },
                                    colors = ButtonDefaults.buttonColors(containerColor = Gold),
                                    modifier = Modifier.weight(1f)
                                ) {
                                    Text(if (piUrlSaved) "SAVED" else "SAVE", color = Color.Black, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                                }
                                OutlinedButton(
                                    onClick = {
                                        piUrlInput = Constants.PI_URL.trimEnd('/')
                                        piUrlSaved = false
                                    },
                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color.Gray),
                                    modifier = Modifier.weight(1f)
                                ) {
                                    Text("RESET", color = Color.Gray, fontSize = 12.sp)
                                }
                            }
                            Button(
                                onClick = {
                                    if (piUrlInput.isNotBlank()) {
                                        PiUrlPreference.set(context, piUrlInput.trim())
                                    }
                                    onPingPi()
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF0D1F0D)),
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp),
                                border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF4CAF50).copy(alpha = 0.7f))
                            ) {
                                Icon(Icons.Default.NetworkCheck, null, tint = Color(0xFF4CAF50), modifier = Modifier.size(15.dp))
                                Spacer(Modifier.width(6.dp))
                                Text(
                                    if (state.piStatus == "PINGING\u2026") "PINGING…" else "PING PI NOW",
                                    color = Color(0xFF4CAF50),
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Bold
                                )
                            }
                            val pingColor = when {
                                state.piStatus == "OK" -> Color(0xFF4CAF50)
                                state.piStatus.startsWith("PINGING") -> Color(0xFFFFD700)
                                else -> Color(0xFFFF5252)
                            }
                            if (state.piStatus != "---") {
                                Text(
                                    "Result: ${state.piStatus}",
                                    color = pingColor,
                                    fontSize = 11.sp,
                                    modifier = Modifier.padding(top = 2.dp)
                                )
                            }
                            Spacer(Modifier.height(4.dp))
                            Button(
                                onClick = {
                                    if (piUrlInput.isNotBlank()) {
                                        PiUrlPreference.set(context, piUrlInput.trim())
                                    }
                                    onForceSync()
                                },
                                enabled = !state.isSyncing,
                                colors = ButtonDefaults.buttonColors(
                                    containerColor = Color(0xFF1A1A1A),
                                    disabledContainerColor = Color(0xFF111111)
                                ),
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp),
                                border = androidx.compose.foundation.BorderStroke(1.dp, Gold.copy(alpha = 0.5f))
                            ) {
                                Icon(Icons.Default.Sync, null, tint = if (state.isSyncing) Color.Gray else Gold, modifier = Modifier.size(16.dp))
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    if (state.isSyncing) "SYNCING..." else "SYNC INVENTORY NOW",
                                    color = if (state.isSyncing) Color.Gray else Gold,
                                    fontSize = 12.sp, fontWeight = FontWeight.Bold
                                )
                            }
                            if (state.lastSyncResult.isNotEmpty()) {
                                Text(
                                    state.lastSyncResult,
                                    color = Color(0xFF4CAF50),
                                    fontSize = 11.sp,
                                    modifier = Modifier.padding(top = 2.dp)
                                )
                            }
                        }
                    }
                }
                // VPN settings
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.VpnKey, null, tint = Color(0xFF7C3AED), modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("DUCKDNS / VPN / REMOTE ADDRESS", color = Color(0xFF7C3AED), fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text(
                                "Save your Pi's DuckDNS, WireGuard, or Tailscale address here. Use the switch buttons to toggle between local LAN and remote access without re-typing.",
                                color = Color.Gray, fontSize = 11.sp, lineHeight = 16.sp
                            )
                            OutlinedTextField(
                                value = vpnUrlInput,
                                onValueChange = { vpnUrlInput = it; vpnUrlSaved = false },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                placeholder = { Text("http://100.x.x.x:8080  or  http://name.ts.net:8080", color = Color.Gray, fontSize = 11.sp) },
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Color(0xFF7C3AED),
                                    unfocusedBorderColor = Color.Gray,
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    cursorColor = Color(0xFF7C3AED)
                                ),
                                textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp)
                            )
                            Button(
                                onClick = {
                                    if (vpnUrlInput.isNotBlank()) {
                                        VpnUrlPreference.set(context, vpnUrlInput.trim())
                                        vpnUrlSaved = true
                                    }
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF7C3AED)),
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp)
                            ) {
                                Text(if (vpnUrlSaved) "VPN ADDRESS SAVED" else "SAVE VPN ADDRESS", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button(
                                    onClick = {
                                        val vpn = VpnUrlPreference.get(context).trimEnd('/')
                                        if (vpn.isNotEmpty() && vpn != "/") {
                                            PiUrlPreference.set(context, vpn)
                                            piUrlInput = vpn
                                            piUrlSaved = true
                                            // Immediately verify the new URL is reachable so
                                            // the operator gets instant feedback instead of
                                            // having to manually press PING PI NOW.
                                            onPingPi()
                                        }
                                    },
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF4C1D95)),
                                    modifier = Modifier.weight(1f),
                                    shape = RoundedCornerShape(6.dp)
                                ) {
                                    Icon(Icons.Default.VpnKey, null, tint = Color.White, modifier = Modifier.size(14.dp))
                                    Spacer(Modifier.width(6.dp))
                                    Text("USE VPN", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                                }
                                Button(
                                    onClick = {
                                        // "Use home LAN" preset — fills the
                                        // hardcoded home WiFi address so the
                                        // operator can flip back from Tailscale
                                        // without re-typing. Auto-pings so the
                                        // operator immediately sees whether the
                                        // device can actually reach the LAN IP
                                        // (it can't if they're not on home WiFi).
                                        PiUrlPreference.set(context, Constants.LAN_URL)
                                        piUrlInput = Constants.LAN_URL.trimEnd('/')
                                        piUrlSaved = true
                                        onPingPi()
                                    },
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1A3A1A)),
                                    modifier = Modifier.weight(1f),
                                    shape = RoundedCornerShape(6.dp),
                                    border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF4CAF50))
                                ) {
                                    Icon(Icons.Default.Router, null, tint = Color(0xFF4CAF50), modifier = Modifier.size(14.dp))
                                    Spacer(Modifier.width(6.dp))
                                    Text("USE HOME LAN", color = Color(0xFF4CAF50), fontWeight = FontWeight.Bold, fontSize = 11.sp)
                                }
                            }
                            // Active URL is read every recomposition so the line
                            // updates instantly whenever USE VPN / USE HOME LAN
                            // is pressed. piUrlSaved is referenced here purely to
                            // make Compose treat it as a recomposition trigger.
                            val _readForRecompose = piUrlSaved
                            Text(
                                "Active: ${PiUrlPreference.get(context).trimEnd('/')}",
                                color = Color(0xFF4CAF50),
                                fontSize = 10.sp,
                                fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                            )
                        }
                    }
                }
                // Venmo settings
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.AccountBalanceWallet, null, tint = Color(0xFF008CFF), modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("VENMO HANDLE", color = Color(0xFF008CFF), fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text("Customers scan a QR code to pay via Venmo.", color = Color.Gray, fontSize = 11.sp)
                            OutlinedTextField(
                                value = venmoHandleInput,
                                onValueChange = { venmoHandleInput = it.trimStart('@'); venmoHandleSaved = false },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                placeholder = { Text("yourhandle  (no @)", color = Color.Gray, fontSize = 12.sp) },
                                leadingIcon = { Text("@", color = Color(0xFF008CFF), fontWeight = FontWeight.Bold) },
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Color(0xFF008CFF),
                                    unfocusedBorderColor = Color.Gray,
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    cursorColor = Color(0xFF008CFF)
                                ),
                                textStyle = androidx.compose.ui.text.TextStyle(fontSize = 13.sp)
                            )
                            Button(
                                onClick = {
                                    VenmoHandlePreference.set(context, venmoHandleInput.trim())
                                    venmoHandleSaved = true
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF008CFF)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(if (venmoHandleSaved) "SAVED ✓" else "SAVE", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }
                        }
                    }
                }

                // Cash App settings
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.AttachMoney, null, tint = Color(0xFF00D632), modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("CASH APP \$CASHTAG", color = Color(0xFF00D632), fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text("Customers scan a QR code to pay via Cash App.", color = Color.Gray, fontSize = 11.sp)
                            OutlinedTextField(
                                value = cashAppHandleInput,
                                onValueChange = { cashAppHandleInput = it.trimStart('$'); cashAppHandleSaved = false },
                                modifier = Modifier.fillMaxWidth(),
                                singleLine = true,
                                placeholder = { Text("YourCashtag  (no \$)", color = Color.Gray, fontSize = 12.sp) },
                                leadingIcon = { Text("\$", color = Color(0xFF00D632), fontWeight = FontWeight.Bold) },
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Color(0xFF00D632),
                                    unfocusedBorderColor = Color.Gray,
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    cursorColor = Color(0xFF00D632)
                                ),
                                textStyle = androidx.compose.ui.text.TextStyle(fontSize = 13.sp)
                            )
                            Button(
                                onClick = {
                                    CashAppHandlePreference.set(context, cashAppHandleInput.trim())
                                    cashAppHandleSaved = true
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF00D632)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(if (cashAppHandleSaved) "SAVED ✓" else "SAVE", color = Color.Black, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }
                        }
                    }
                }

                // Tax & Transaction Fee settings
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Payments, null, tint = Gold, modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("TAX & TRANSACTION FEES", color = Gold, fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text(
                                "Tax is added to every sale. Transaction fee (e.g. PayPal 2.38%) is shown at checkout as a separate line — not added to Zettle or cash totals.",
                                color = Color.Gray, fontSize = 11.sp
                            )
                            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text("Sales Tax %", color = Color.LightGray, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    OutlinedTextField(
                                        value = taxRateInput,
                                        onValueChange = { taxRateInput = it; ratesSaved = false },
                                        modifier = Modifier.fillMaxWidth(),
                                        singleLine = true,
                                        placeholder = { Text("5.6", color = Color.Gray, fontSize = 12.sp) },
                                        trailingIcon = { Text("%", color = Gold, fontWeight = FontWeight.Bold, fontSize = 13.sp) },
                                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedBorderColor = Gold,
                                            unfocusedBorderColor = Color.Gray,
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            cursorColor = Gold
                                        ),
                                        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 14.sp)
                                    )
                                }
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text("PayPal Fee %", color = Color.LightGray, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    OutlinedTextField(
                                        value = feeRateInput,
                                        onValueChange = { feeRateInput = it; ratesSaved = false },
                                        modifier = Modifier.fillMaxWidth(),
                                        singleLine = true,
                                        placeholder = { Text("2.38", color = Color.Gray, fontSize = 12.sp) },
                                        trailingIcon = { Text("%", color = Color(0xFF0070BA), fontWeight = FontWeight.Bold, fontSize = 13.sp) },
                                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedBorderColor = Color(0xFF0070BA),
                                            unfocusedBorderColor = Color.Gray,
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            cursorColor = Color(0xFF0070BA)
                                        ),
                                        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 14.sp)
                                    )
                                }
                            }
                            Button(
                                onClick = {
                                    val taxPct = taxRateInput.toFloatOrNull() ?: 5.6f
                                    val feePct = feeRateInput.toFloatOrNull() ?: 2.38f
                                    TaxRatePreference.set(context, taxPct)
                                    TransactionFeePreference.set(context, feePct)
                                    taxRateInput = taxPct.toString()
                                    feeRateInput = feePct.toString()
                                    ratesSaved = true
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Gold),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(if (ratesSaved) "SAVED ✓" else "SAVE RATES", color = VaultBlack, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }
                        }
                    }
                }

                item {
                    Surface(
                        shape = RoundedCornerShape(16.dp),
                        color = Color(0xFF1A1A1A),
                        tonalElevation = 0.dp
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.SwapHoriz, null, tint = Color(0xFFF59E0B), modifier = Modifier.size(20.dp))
                                Spacer(Modifier.width(10.dp))
                                Text("TRADE-IN BUY RATES", color = Color(0xFFF59E0B), fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            }
                            Text(
                                "Set the percentage of market value you offer when buying cards. Cash Buy is for cash payouts. Store Credit is for trade credit toward purchases.",
                                color = Color.Gray, fontSize = 11.sp
                            )
                            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text("Cash Buy %", color = Color.LightGray, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    OutlinedTextField(
                                        value = buyPctInput,
                                        onValueChange = { buyPctInput = it; tradePctSaved = false },
                                        modifier = Modifier.fillMaxWidth(),
                                        singleLine = true,
                                        placeholder = { Text("80", color = Color.Gray, fontSize = 12.sp) },
                                        trailingIcon = { Text("%", color = Color(0xFFF59E0B), fontWeight = FontWeight.Bold, fontSize = 13.sp) },
                                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedBorderColor = Color(0xFFF59E0B),
                                            unfocusedBorderColor = Color.Gray,
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            cursorColor = Color(0xFFF59E0B)
                                        ),
                                        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 14.sp)
                                    )
                                }
                                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text("Store Credit %", color = Color.LightGray, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    OutlinedTextField(
                                        value = creditPctInput,
                                        onValueChange = { creditPctInput = it; tradePctSaved = false },
                                        modifier = Modifier.fillMaxWidth(),
                                        singleLine = true,
                                        placeholder = { Text("80", color = Color.Gray, fontSize = 12.sp) },
                                        trailingIcon = { Text("%", color = Color(0xFF4ADE80), fontWeight = FontWeight.Bold, fontSize = 13.sp) },
                                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedBorderColor = Color(0xFF4ADE80),
                                            unfocusedBorderColor = Color.Gray,
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            cursorColor = Color(0xFF4ADE80)
                                        ),
                                        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 14.sp)
                                    )
                                }
                            }
                            Text(
                                "Current: Cash ${buyPctInput}% | Credit ${creditPctInput}% of market value",
                                color = Color(0xFFF59E0B), fontSize = 11.sp, fontWeight = FontWeight.Medium
                            )
                            Button(
                                onClick = {
                                    val buyVal = (buyPctInput.toFloatOrNull() ?: 80f).coerceIn(10f, 100f)
                                    val creditVal = (creditPctInput.toFloatOrNull() ?: 80f).coerceIn(10f, 100f)
                                    BuyPricePreference.set(context, buyVal / 100f)
                                    TradeCreditPreference.set(context, creditVal / 100f)
                                    onSetTradeBuyPct(buyVal.toDouble() / 100.0)
                                    onSetTradeCreditPct(creditVal.toDouble() / 100.0)
                                    buyPctInput = "%.0f".format(buyVal)
                                    creditPctInput = "%.0f".format(creditVal)
                                    tradePctSaved = true
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(if (tradePctSaved) "SAVED ✓" else "SAVE TRADE RATES", color = VaultBlack, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }

                            Spacer(Modifier.height(16.dp))
                            Text("SIGNING SCREEN DISCLOSURE", color = Color(0xFFF59E0B), fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.sp)
                            Text("Shown at the bottom of the customer signing screen.", color = Color(0xFF888888), fontSize = 11.sp)
                            Spacer(Modifier.height(8.dp))
                            OutlinedTextField(
                                value = disclosureInput,
                                onValueChange = { disclosureInput = it },
                                modifier = Modifier.fillMaxWidth(),
                                minLines = 3,
                                maxLines = 6,
                                placeholder = { Text("Enter trade-in disclosure / terms text…", color = Color(0xFF555555), fontSize = 12.sp) },
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Color(0xFFF59E0B),
                                    unfocusedBorderColor = Color(0xFF333333),
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color(0xFFCCCCCC),
                                    cursorColor = Color(0xFFF59E0B)
                                ),
                                textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp)
                            )
                            Spacer(Modifier.height(8.dp))
                            Button(
                                onClick = { onUpdateCustomerDisplayDisclosure(disclosureInput) },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B)),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text("SAVE DISCLOSURE", color = VaultBlack, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                            }
                        }
                    }
                }

                item {
                    StatusRow(
                        label = "WIREGUARD VPN TUNNEL",
                        status = if (state.piStatus != "OFFLINE") "ACTIVE" else "DISCONNECTED",
                        isOnline = state.piStatus != "OFFLINE",
                        icon = Icons.Default.Security
                    )
                }
                item {
                    StatusRow(
                        label = "REPLIT INVENTORY HUB",
                        status = "SYNCED",
                        isOnline = true,
                        icon = Icons.Default.CloudSync
                    )
                }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Row(
                            Modifier.fillMaxWidth().padding(16.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.weight(1f)) {
                                Icon(Icons.Default.QrCodeScanner, null, tint = Gold, modifier = Modifier.size(24.dp))
                                Spacer(Modifier.width(16.dp))
                                Column {
                                    Text("EXPO SCANNER APP", color = Color.White, fontWeight = FontWeight.Bold)
                                    Text(
                                        Constants.REMOTE_SCANNER_URL.removePrefix("https://"),
                                        color = Color.Gray, fontSize = 10.sp
                                    )
                                }
                            }
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Surface(
                                    color = when {
                                        state.scannerAppStatus == "ONLINE" -> Color(0xFF2E7D32)
                                        state.scannerAppStatus == "---" -> Color(0xFF333333)
                                        else -> Color(0xFFC62828)
                                    },
                                    shape = RoundedCornerShape(4.dp)
                                ) {
                                    Text(
                                        state.scannerAppStatus,
                                        color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Black,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                                    )
                                }
                                IconButton(
                                    onClick = {
                                        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(Constants.REMOTE_SCANNER_URL))
                                        context.startActivity(intent)
                                    },
                                    modifier = Modifier.size(32.dp)
                                ) {
                                    Icon(Icons.Default.OpenInNew, null, tint = Gold, modifier = Modifier.size(16.dp))
                                }
                            }
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("BLUETOOTH PRINTER") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.fillMaxWidth().padding(16.dp)) {
                            Row(
                                Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.weight(1f)) {
                                    Icon(Icons.Default.Print, null, tint = Gold, modifier = Modifier.size(24.dp))
                                    Spacer(Modifier.width(16.dp))
                                    Column {
                                        Text(
                                            if (state.printerName.isNotEmpty()) state.printerName else "No Printer Selected",
                                            color = Color.White, fontWeight = FontWeight.Bold
                                        )
                                        Text(
                                            if (state.printerAddress.isNotEmpty()) state.printerAddress else "Tap to select a paired printer",
                                            color = Color.Gray, fontSize = 11.sp
                                        )
                                    }
                                }
                                Surface(
                                    color = when (state.printerStatus) {
                                        "PAIRED" -> Color(0xFF2E7D32)
                                        "---" -> Color(0xFF333333)
                                        else -> Color(0xFFC62828)
                                    },
                                    shape = RoundedCornerShape(4.dp)
                                ) {
                                    Text(
                                        state.printerStatus,
                                        color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Black,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                                    )
                                }
                            }
                            Spacer(Modifier.height(12.dp))

                            // ── Transport mode toggle (BT / USB / Ethernet) ──
                            // The MUNBYN P047 supports USB & Ethernet; legacy
                            // BT receipt printers stay on the BT path.
                            Text(
                                "CONNECTION TYPE",
                                color = Color.White.copy(alpha = 0.6f),
                                fontSize = 10.sp,
                                fontWeight = FontWeight.Bold,
                                letterSpacing = 1.sp
                            )
                            Spacer(Modifier.height(6.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(6.dp)
                            ) {
                                listOf("BT" to "Bluetooth", "USB" to "Tablet USB", "ETH" to "Ethernet", "PI" to "Pi USB").forEach { (key, label) ->
                                    val on = state.printerMode == key
                                    Button(
                                        onClick = { onSetPrinterMode(key) },
                                        modifier = Modifier.weight(1f),
                                        shape = RoundedCornerShape(6.dp),
                                        colors = ButtonDefaults.buttonColors(
                                            containerColor = if (on) Gold else VaultBlack
                                        )
                                    ) {
                                        Text(
                                            label,
                                            color = if (on) VaultBlack else Color.White,
                                            fontSize = 12.sp,
                                            fontWeight = FontWeight.Bold
                                        )
                                    }
                                }
                            }

                            Spacer(Modifier.height(12.dp))

                            // ── Per-mode config UI ───────────────────────────
                            when (state.printerMode) {
                                "ETH" -> {
                                    var host by remember(state.printerEthHost) {
                                        mutableStateOf(state.printerEthHost)
                                    }
                                    var portStr by remember(state.printerEthPort) {
                                        mutableStateOf(state.printerEthPort.toString())
                                    }
                                    OutlinedTextField(
                                        value = host,
                                        onValueChange = { host = it },
                                        label = { Text("Printer IP", color = Color.White.copy(alpha = 0.7f)) },
                                        placeholder = { Text("192.168.1.87", color = Color.White.copy(alpha = 0.4f)) },
                                        singleLine = true,
                                        modifier = Modifier.fillMaxWidth(),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            focusedBorderColor = Gold,
                                            unfocusedBorderColor = Color.White.copy(alpha = 0.3f)
                                        )
                                    )
                                    Spacer(Modifier.height(8.dp))
                                    OutlinedTextField(
                                        value = portStr,
                                        onValueChange = { portStr = it.filter { c -> c.isDigit() }.take(5) },
                                        label = { Text("Port (default 9100)", color = Color.White.copy(alpha = 0.7f)) },
                                        singleLine = true,
                                        modifier = Modifier.fillMaxWidth(),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            focusedBorderColor = Gold,
                                            unfocusedBorderColor = Color.White.copy(alpha = 0.3f)
                                        )
                                    )
                                    Spacer(Modifier.height(8.dp))
                                    Button(
                                        onClick = {
                                            val p = portStr.toIntOrNull() ?: 9100
                                            onSetPrinterEthernet(host.trim(), p)
                                        },
                                        enabled = host.isNotBlank(),
                                        colors = ButtonDefaults.buttonColors(containerColor = VaultBlack),
                                        modifier = Modifier.fillMaxWidth(),
                                        shape = RoundedCornerShape(6.dp)
                                    ) {
                                        Icon(Icons.Default.Save, null, tint = Gold, modifier = Modifier.size(16.dp))
                                        Spacer(Modifier.width(8.dp))
                                        Text("SAVE NETWORK ADDRESS",
                                            color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    }
                                    Spacer(Modifier.height(6.dp))
                                    Text(
                                        "Tip: hold FEED while powering the MUNBYN P047 on — it prints a slip with its IP. Tablet & printer must share the same Wi-Fi network.",
                                        color = Color.White.copy(alpha = 0.55f),
                                        fontSize = 11.sp,
                                        lineHeight = 14.sp
                                    )
                                }
                                "USB" -> {
                                    Text(
                                        "Connect the MUNBYN P047 to your tablet's USB-C port via an OTG cable. Android will ask for permission the first time you print.",
                                        color = Color.White.copy(alpha = 0.7f),
                                        fontSize = 12.sp,
                                        lineHeight = 16.sp
                                    )
                                }
                                "PI" -> {
                                    // Pi-routed print: receipt jobs are sent over the
                                    // network to the Pi backend, which forwards them
                                    // to whatever printer is plugged into the Pi
                                    // (e.g. MUNBYN P047 on /dev/usb/lp0). Use this
                                    // when the printer is NOT connected to the
                                    // tablet at all. Use the LIVE-TEST button below
                                    // to confirm the Pi can reach the printer.
                                    Text(
                                        "Receipts will be sent to the Pi (Tailscale 100.125.5.34) and printed by whatever printer is plugged into the Pi's USB port. Nothing needs to be paired with this tablet.",
                                        color = Color.White.copy(alpha = 0.7f),
                                        fontSize = 12.sp,
                                        lineHeight = 16.sp
                                    )
                                }
                                else -> {
                                    Button(
                                        onClick = { showPrinterPicker = true },
                                        colors = ButtonDefaults.buttonColors(containerColor = VaultBlack),
                                        modifier = Modifier.fillMaxWidth(),
                                        shape = RoundedCornerShape(6.dp)
                                    ) {
                                        Icon(Icons.Default.Bluetooth, null, tint = Gold, modifier = Modifier.size(16.dp))
                                        Spacer(Modifier.width(8.dp))
                                        Text(
                                            if (state.printerAddress.isEmpty()) "SELECT PAIRED PRINTER" else "CHANGE PAIRED PRINTER",
                                            color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold
                                        )
                                    }
                                }
                            }

                            Spacer(Modifier.height(10.dp))

                            // ── LIVE-TEST button ─────────────────────────────
                            // Probes the chosen transport and updates the
                            // status pill above with OK or FAIL: <reason>.
                            val statusUpper = state.printerStatus.uppercase()
                            val testColor = when {
                                statusUpper == "OK"            -> Color(0xFF1B5E20)  // green
                                statusUpper.startsWith("FAIL") -> Color(0xFFB71C1C)  // red
                                else                           -> Color(0xFF2E2E2E)  // neutral
                            }
                            Button(
                                onClick = { onTestPrinter() },
                                colors = ButtonDefaults.buttonColors(containerColor = testColor),
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp)
                            ) {
                                Icon(Icons.Default.PlayArrow, null, tint = Gold, modifier = Modifier.size(16.dp))
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    when {
                                        statusUpper == "OK"            -> "PRINTER LIVE — TEST AGAIN"
                                        statusUpper.startsWith("FAIL") -> "OFFLINE — TAP TO RETRY"
                                        else                           -> "TEST PRINTER CONNECTION"
                                    },
                                    color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold
                                )
                            }

                            Spacer(Modifier.height(8.dp))

                            // ── DRAWER KICK TEST ─────────────────────────────
                            // Pulses the cash drawer pin without printing a
                            // visible receipt. Distinct from the printer test
                            // above (which only verifies the print path).
                            Button(
                                onClick = { onTestKickDrawer() },
                                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E2E2E)),
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(6.dp)
                            ) {
                                Icon(Icons.Default.PointOfSale, null, tint = Gold, modifier = Modifier.size(16.dp))
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    "TEST CASH DRAWER",
                                    color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold
                                )
                            }
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("RECEIPT LAYOUT") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            ReceiptLayout.values().forEach { layout ->
                                val isSelected = state.receiptLayout == layout
                                Row(
                                    Modifier
                                        .fillMaxWidth()
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(if (isSelected) Color(0xFF1A1400) else Color.Transparent)
                                        .border(
                                            width = if (isSelected) 1.dp else 0.dp,
                                            color = if (isSelected) Gold else Color.Transparent,
                                            shape = RoundedCornerShape(8.dp)
                                        )
                                        .clickable { onSetReceiptLayout(layout) }
                                        .padding(12.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    RadioButton(
                                        selected = isSelected,
                                        onClick = { onSetReceiptLayout(layout) },
                                        colors = RadioButtonDefaults.colors(selectedColor = Gold)
                                    )
                                    Spacer(Modifier.width(8.dp))
                                    Column {
                                        Text(
                                            layout.name,
                                            color = if (isSelected) Gold else Color.White,
                                            fontWeight = FontWeight.Bold, fontSize = 14.sp
                                        )
                                        Text(
                                            when (layout) {
                                                ReceiptLayout.MINIMAL  -> "Items + total only. Fast, clean."
                                                ReceiptLayout.STANDARD -> "Header, itemized list, subtotal, tax, total."
                                                ReceiptLayout.DETAILED -> "Full breakdown — tip, payment method, change, branding."
                                            },
                                            color = Color.Gray, fontSize = 11.sp
                                        )
                                    }
                                }
                            }
                        }
                    }
                }

                item { SectionHeader("RECEIPT QR CODE") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.fillMaxWidth().padding(16.dp)) {
                            Row(
                                Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.QrCode2, null, tint = Gold, modifier = Modifier.size(24.dp))
                                    Spacer(Modifier.width(12.dp))
                                    Column {
                                        Text("PRINT QR ON RECEIPT", color = Color.White, fontWeight = FontWeight.Bold)
                                        Text("Prints at the bottom of each receipt", color = Color.Gray, fontSize = 11.sp)
                                    }
                                }
                                Switch(
                                    checked = state.receiptQrEnabled,
                                    onCheckedChange = { onToggleReceiptQr() },
                                    colors = SwitchDefaults.colors(checkedThumbColor = Gold, checkedTrackColor = Color(0xFF5A3D00))
                                )
                            }
                            if (state.receiptQrEnabled) {
                                Spacer(Modifier.height(12.dp))
                                OutlinedTextField(
                                    value = qrInput,
                                    onValueChange = { qrInput = it; onUpdateQrData(it) },
                                    label = { Text("QR CODE URL / TEXT", color = Color.Gray, fontSize = 12.sp) },
                                    modifier = Modifier.fillMaxWidth(),
                                    singleLine = true,
                                    leadingIcon = { Icon(Icons.Default.Link, null, tint = Color.Gray, modifier = Modifier.size(18.dp)) },
                                    colors = OutlinedTextFieldDefaults.colors(
                                        focusedTextColor = Color.White,
                                        unfocusedTextColor = Color.White,
                                        focusedBorderColor = Gold,
                                        unfocusedBorderColor = Color(0xFF444444)
                                    )
                                )
                            }
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("RECEIPT LOGO") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.fillMaxWidth().padding(16.dp)) {
                            // Where receipts pull the logo from. Stored in
                            // app-private filesDir so no permission is needed
                            // and it survives reboots / Pi outages.
                            val ctx = LocalContext.current
                            val logoFile = remember { java.io.File(ctx.filesDir, "receipt_logo.png") }
                            val ioScope = rememberCoroutineScope()
                            // Trigger recompositions when the file changes.
                            var logoRev by remember { mutableStateOf(0) }
                            val hasLogo = remember(logoRev) { logoFile.exists() && logoFile.length() > 0 }
                            val logoBitmap = remember(logoRev) {
                                if (hasLogo) android.graphics.BitmapFactory.decodeFile(logoFile.absolutePath) else null
                            }
                            // Recycle the previous preview bitmap when this card
                            // leaves composition or the user picks a new logo.
                            // Without this, the native pixel buffer (multi-MB)
                            // lingers until the next GC and repeated picks can
                            // OOM on lower-end tablets.
                            DisposableEffect(logoBitmap) {
                                onDispose { logoBitmap?.takeIf { !it.isRecycled }?.recycle() }
                            }

                            val pickLogo = androidx.activity.compose.rememberLauncherForActivityResult(
                                androidx.activity.result.contract.ActivityResultContracts.PickVisualMedia()
                            ) { uri ->
                                if (uri == null) return@rememberLauncherForActivityResult
                                // Decode + downscale + write happen off the main
                                // thread to avoid an ANR on large gallery images.
                                ioScope.launch(kotlinx.coroutines.Dispatchers.IO) {
                                    runCatching {
                                        val src = ctx.contentResolver.openInputStream(uri)?.use {
                                            android.graphics.BitmapFactory.decodeStream(it)
                                        } ?: return@runCatching
                                        // Cap BOTH width (576 px = 80mm raster) and
                                        // height (800 px ≈ 4 in tall) so a portrait
                                        // image can't blow past the 600 KB receipt
                                        // payload cap and silently disappear from
                                        // the print. Aspect ratio is preserved.
                                        val maxW = 576
                                        val maxH = 800
                                        val ratio = minOf(
                                            1f,
                                            maxW.toFloat() / src.width,
                                            maxH.toFloat() / src.height
                                        )
                                        val scaled = if (ratio < 1f) {
                                            android.graphics.Bitmap.createScaledBitmap(
                                                src,
                                                (src.width * ratio).toInt().coerceAtLeast(1),
                                                (src.height * ratio).toInt().coerceAtLeast(1),
                                                true
                                            )
                                        } else src
                                        java.io.FileOutputStream(logoFile).use { fos ->
                                            scaled.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, fos)
                                        }
                                        if (scaled !== src) scaled.recycle()
                                        src.recycle()
                                    }
                                    withContext(kotlinx.coroutines.Dispatchers.Main) { logoRev++ }
                                }
                            }

                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Image, null, tint = Gold, modifier = Modifier.size(22.dp))
                                Spacer(Modifier.width(10.dp))
                                Column(Modifier.weight(1f)) {
                                    Text("STORE LOGO", color = Color.White, fontWeight = FontWeight.Bold)
                                    Text("Printed at the top of every receipt", color = Color.Gray, fontSize = 11.sp)
                                }
                            }

                            Spacer(Modifier.height(12.dp))

                            if (logoBitmap != null) {
                                // White card so dark logos remain visible — and
                                // it mimics what the customer will see on
                                // the thermal paper.
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .height(140.dp)
                                        .background(Color.White, RoundedCornerShape(6.dp))
                                        .padding(12.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    androidx.compose.foundation.Image(
                                        bitmap = logoBitmap.asImageBitmap(),
                                        contentDescription = "Receipt logo preview",
                                        modifier = Modifier.fillMaxSize(),
                                        contentScale = androidx.compose.ui.layout.ContentScale.Fit
                                    )
                                }
                                Spacer(Modifier.height(10.dp))
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Button(
                                        onClick = {
                                            pickLogo.launch(
                                                androidx.activity.result.PickVisualMediaRequest(
                                                    androidx.activity.result.contract.ActivityResultContracts.PickVisualMedia.ImageOnly
                                                )
                                            )
                                        },
                                        modifier = Modifier.weight(1f),
                                        colors = ButtonDefaults.buttonColors(containerColor = VaultBlack),
                                        shape = RoundedCornerShape(6.dp)
                                    ) {
                                        Icon(Icons.Default.Edit, null, tint = Gold, modifier = Modifier.size(16.dp))
                                        Spacer(Modifier.width(8.dp))
                                        Text("REPLACE", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    }
                                    Button(
                                        onClick = {
                                            logoFile.delete()
                                            logoRev++
                                        },
                                        modifier = Modifier.weight(1f),
                                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF7A1F1F)),
                                        shape = RoundedCornerShape(6.dp)
                                    ) {
                                        Icon(Icons.Default.Delete, null, tint = Color.White, modifier = Modifier.size(16.dp))
                                        Spacer(Modifier.width(8.dp))
                                        Text("REMOVE", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                    }
                                }
                            } else {
                                Button(
                                    onClick = {
                                        pickLogo.launch(
                                            androidx.activity.result.PickVisualMediaRequest(
                                                androidx.activity.result.contract.ActivityResultContracts.PickVisualMedia.ImageOnly
                                            )
                                        )
                                    },
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonDefaults.buttonColors(containerColor = VaultBlack),
                                    shape = RoundedCornerShape(6.dp)
                                ) {
                                    Icon(Icons.Default.Image, null, tint = Gold, modifier = Modifier.size(16.dp))
                                    Spacer(Modifier.width(8.dp))
                                    Text("UPLOAD LOGO IMAGE", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                                }
                                Spacer(Modifier.height(6.dp))
                                Text(
                                    "PNG with transparent background works best. Width auto-scales to 384px (≈ 50mm).",
                                    color = Color.White.copy(alpha = 0.55f),
                                    fontSize = 11.sp
                                )
                            }
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("RECEIPT DISCLAIMER") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.fillMaxWidth().padding(16.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Default.Gavel, null, tint = Gold, modifier = Modifier.size(22.dp))
                                Spacer(Modifier.width(10.dp))
                                Column {
                                    Text("DISCLAIMER / FOOTER TEXT", color = Color.White, fontWeight = FontWeight.Bold)
                                    Text("Printed at the bottom of every receipt", color = Color.Gray, fontSize = 11.sp)
                                }
                            }
                            Spacer(Modifier.height(12.dp))
                            OutlinedTextField(
                                value = disclaimerInput,
                                onValueChange = { disclaimerInput = it },
                                placeholder = { Text("e.g. All sales final. No refunds on opened packs.", color = Color(0xFF555555), fontSize = 12.sp) },
                                modifier = Modifier.fillMaxWidth().heightIn(min = 80.dp),
                                maxLines = 5,
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedTextColor = Color.White,
                                    unfocusedTextColor = Color.White,
                                    focusedBorderColor = Gold,
                                    unfocusedBorderColor = Color(0xFF444444)
                                )
                            )
                            Spacer(Modifier.height(4.dp))
                            Text("Tap SAVE SETTINGS below to apply", color = Color.Gray, fontSize = 10.sp)
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("QUICK SALE BUTTONS") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(
                            Modifier.fillMaxWidth().padding(16.dp),
                            verticalArrangement = Arrangement.spacedBy(12.dp)
                        ) {
                            val presetIcons = listOf(
                                Icons.Default.Style,
                                Icons.Default.Stars,
                                Icons.Default.ShoppingBag
                            )
                            state.quickSalePresets.forEachIndexed { i, preset ->
                                var labelInput by remember(preset.label) { mutableStateOf(preset.label) }
                                Row(
                                    Modifier.fillMaxWidth(),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(12.dp)
                                ) {
                                    Icon(
                                        presetIcons.getOrElse(i) { Icons.Default.Add },
                                        null,
                                        tint = Gold,
                                        modifier = Modifier.size(22.dp)
                                    )
                                    OutlinedTextField(
                                        value = labelInput,
                                        onValueChange = {
                                            labelInput = it
                                            // Keep lifted list in sync so SAVE button can flush
                                            if (i < quickSaleInputs.size) quickSaleInputs[i] = it
                                        },
                                        label = { Text("Button ${i + 1}", color = Color.Gray, fontSize = 11.sp) },
                                        singleLine = true,
                                        modifier = Modifier.weight(1f),
                                        colors = OutlinedTextFieldDefaults.colors(
                                            focusedTextColor = Color.White,
                                            unfocusedTextColor = Color.White,
                                            focusedBorderColor = Gold,
                                            unfocusedBorderColor = Color(0xFF444444)
                                        ),
                                        trailingIcon = {
                                            if (labelInput != preset.label) {
                                                IconButton(onClick = { onUpdateQuickSalePreset(i, labelInput) }) {
                                                    Icon(Icons.Default.Check, null, tint = Gold, modifier = Modifier.size(18.dp))
                                                }
                                            }
                                        }
                                    )
                                }
                            }
                            Text(
                                "Tap the checkmark to save each label",
                                color = Color.Gray, fontSize = 10.sp,
                                modifier = Modifier.padding(top = 4.dp)
                            )
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("HARDWARE CONFIG") }
                item {
                    ToggleSetting(
                        label = "CASH DRAWER AUTO-PULSE",
                        checked = state.isCashDrawerEnabled,
                        onCheckedChange = onToggleDrawer
                    )
                }
                // Pi-side drawer kick: tells the Pi to pulse the cash-drawer
                // pin (via its USB receipt printer) right before cutting on
                // EVERY sale, regardless of payment method. The toggle above
                // only fires for local CASH payments through the tablet's own
                // BT/USB printer; this one applies to receipts routed via
                // /print/receipt on the Pi.
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp)
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(
                                    "Auto-open cash drawer on every sale",
                                    color = Color.White,
                                    fontSize = 14.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Spacer(Modifier.height(2.dp))
                                Text(
                                    "Pulses the drawer pin via the printer when a receipt prints.",
                                    color = Color.White.copy(alpha = 0.6f),
                                    fontSize = 11.sp,
                                    lineHeight = 14.sp
                                )
                            }
                            Switch(
                                checked = state.isKickDrawerEnabled,
                                onCheckedChange = onToggleKickDrawer,
                                colors = SwitchDefaults.colors(
                                    checkedThumbColor = Gold,
                                    checkedTrackColor = Gold.copy(alpha = 0.5f),
                                    uncheckedThumbColor = Color.White.copy(alpha = 0.6f),
                                    uncheckedTrackColor = Color.White.copy(alpha = 0.2f)
                                )
                            )
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("OPENING CASH FLOAT") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(containerColor = VaultSurface),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Text(
                                "Set the cash in the drawer at open. Used for Z-Report variance calculation.",
                                color = Color.Gray,
                                fontSize = 11.sp
                            )
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(12.dp)
                            ) {
                                Text(
                                    if (state.openingFloat > 0.0) "$${"%.2f".format(state.openingFloat)}" else "Not set",
                                    color = if (state.openingFloat > 0.0) Gold else Color(0xFF666666),
                                    fontSize = 22.sp,
                                    fontWeight = FontWeight.Black,
                                    modifier = Modifier.weight(1f)
                                )
                                Button(
                                    onClick = {
                                        floatDialogInput = if (state.openingFloat > 0.0) "%.2f".format(state.openingFloat) else ""
                                        showFloatDialog = true
                                        floatSaved = false
                                    },
                                    colors = ButtonDefaults.buttonColors(
                                        containerColor = if (floatSaved) Color(0xFF1B5E20) else Gold
                                    ),
                                    shape = RoundedCornerShape(6.dp)
                                ) {
                                    Text(
                                        if (floatSaved) "SAVED ✓" else "SET FLOAT",
                                        color = Color.Black,
                                        fontWeight = FontWeight.Bold,
                                        fontSize = 12.sp
                                    )
                                }
                            }
                        }
                    }
                }

                item { Spacer(Modifier.height(8.dp)) }
                item { SectionHeader("REGISTER LOCK") }
                item {
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = if (state.isRegisterMode) Color(0xFF1A0F00) else VaultSurface
                        ),
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Row(
                            Modifier.fillMaxWidth().padding(16.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(
                                    if (state.isRegisterMode) Icons.Default.Lock else Icons.Default.LockOpen,
                                    null,
                                    tint = if (state.isRegisterMode) Color(0xFFFF6B00) else Gold,
                                    modifier = Modifier.size(24.dp)
                                )
                                Spacer(Modifier.width(16.dp))
                                Column {
                                    Text(
                                        if (state.isRegisterMode) "REGISTER MODE ACTIVE" else "REGISTER MODE",
                                        color = Color.White, fontWeight = FontWeight.Bold
                                    )
                                    Text(
                                        if (state.isRegisterMode) "App locked — PIN required to exit" else "Lock app during sales (PIN: 1234)",
                                        color = Color.Gray, fontSize = 11.sp
                                    )
                                }
                            }
                            Switch(
                                checked = state.isRegisterMode,
                                onCheckedChange = {
                                    if (state.isRegisterMode) showPinDialog = true
                                    else onToggleRegisterMode()
                                },
                                colors = SwitchDefaults.colors(
                                    checkedThumbColor = Color(0xFFFF6B00),
                                    checkedTrackColor = Color(0xFF5A2D00)
                                )
                            )
                        }
                    }
                }

                item { Spacer(Modifier.height(24.dp)) }

                item {
                    Button(
                        onClick = {
                            // Flush all pending changes to persistent storage
                            onUpdateQrData(qrInput)
                            onUpdateDisclaimer(disclaimerInput)
                            quickSaleInputs.forEachIndexed { i, label ->
                                if (i < state.quickSalePresets.size && label != state.quickSalePresets[i].label) {
                                    onUpdateQuickSalePreset(i, label)
                                }
                            }
                            savedBanner = true
                            scope.launch {
                                delay(2500)
                                savedBanner = false
                            }
                        },
                        modifier = Modifier.fillMaxWidth().height(56.dp),
                        shape = RoundedCornerShape(12.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = Gold)
                    ) {
                        Icon(Icons.Default.Save, null, tint = VaultBlack, modifier = Modifier.size(20.dp))
                        Spacer(Modifier.width(10.dp))
                        Text("SAVE SETTINGS", color = VaultBlack, fontWeight = FontWeight.Black, fontSize = 15.sp, letterSpacing = 1.sp)
                    }
                }

                item { Spacer(Modifier.height(16.dp)) }
            }
        }
    }
}

@Composable
fun SectionHeader(title: String) {
    Text(
        title,
        color = Color.Gray, fontSize = 11.sp, fontWeight = FontWeight.Bold,
        letterSpacing = 2.sp, modifier = Modifier.padding(bottom = 4.dp)
    )
}

@Composable
fun StatusRow(label: String, status: String, isOnline: Boolean, icon: androidx.compose.ui.graphics.vector.ImageVector) {
    Card(
        colors = CardDefaults.cardColors(containerColor = VaultSurface),
        shape = RoundedCornerShape(8.dp)
    ) {
        Row(
            Modifier.fillMaxWidth().padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, null, tint = Gold, modifier = Modifier.size(24.dp))
                Spacer(Modifier.width(16.dp))
                Text(label, color = Color.White, fontWeight = FontWeight.Bold)
            }
            Surface(
                color = if (isOnline) Color(0xFF2E7D32) else Color(0xFFC62828),
                shape = RoundedCornerShape(4.dp)
            ) {
                Text(
                    status, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Black,
                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                )
            }
        }
    }
}


@Composable
fun ToggleSetting(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 8.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, color = Color.White, fontWeight = FontWeight.Medium)
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(checkedThumbColor = Gold)
        )
    }
}
