package com.hanryxvault.pos

import android.content.Context
import android.util.Log
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.io.IOException
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

// ── Pi URL rewriter for the local inventory ApiService ────────────────────────
// Rewrites default PI_URL host to whatever address is saved in Settings.
class DynamicPiUrlInterceptor(private val context: Context) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()

        val defaultPiHost = try {
            Constants.PI_URL.trimEnd('/').toHttpUrl().host
        } catch (e: Exception) {
            return chain.proceed(original)
        }

        val savedPiUrl = try {
            PiUrlPreference.get(context).trimEnd('/').toHttpUrl()
        } catch (e: Exception) {
            Constants.PI_URL.trimEnd('/').toHttpUrl()
        }

        val isPiRequest = original.url.host == defaultPiHost || original.url.host == savedPiUrl.host
        if (!isPiRequest) return chain.proceed(original)

        val newUrl = original.url.newBuilder()
            .scheme(savedPiUrl.scheme)
            .host(savedPiUrl.host)
            .port(savedPiUrl.port)
            .build()
        return chain.proceed(original.newBuilder().url(newUrl).build())
    }
}

// ── Pi-first / cloud-fallback interceptor for ZettleServerApi ─────────────────
// Every request is tried against the Pi first (fast 3 s connect timeout).
// Falls back to Replit cloud on:
//   • IOException / timeout  (Pi unreachable)
//   • HTTP 404               (endpoint exists on cloud but not Pi)
//   • HTTP 5xx               (Pi server error)
// All other responses (2xx, 3xx, 4xx except 404) are returned as-is from Pi.
class PiFirstCloudFallbackInterceptor(private val context: Context) : Interceptor {

    private val cloudClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(45, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    // Hostname of the cloud server — strip scheme and trailing slash
    private val cloudHost by lazy {
        Constants.ZETTLE_SERVER_URL
            .removePrefix("https://")
            .removePrefix("http://")
            .trimEnd('/')
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()

        // Resolve Pi URL from saved preference
        val piHttpUrl = try {
            PiUrlPreference.get(context).trimEnd('/').toHttpUrl()
        } catch (e: Exception) {
            try { Constants.PI_URL.trimEnd('/').toHttpUrl() }
            catch (_: Exception) { return chain.proceed(original) }
        }

        // Rewrite the request to target the Pi
        val piUrl = original.url.newBuilder()
            .scheme(piHttpUrl.scheme)
            .host(piHttpUrl.host)
            .port(piHttpUrl.port)
            .build()
        val piRequest = original.newBuilder().url(piUrl).build()

        val piResponse = try {
            chain.proceed(piRequest)
        } catch (e: IOException) {
            // Pi unreachable — fall back to cloud
            Log.w("PiFirst", "Pi unreachable (${e.message}) — routing to cloud: ${original.url.encodedPath}")
            return fallbackToCloud(original)
        }

        // Pi is alive but endpoint not found (cloud-only route) or server error
        return if (piResponse.code == 404 || piResponse.code >= 500) {
            piResponse.close()
            Log.d("PiFirst", "Pi HTTP ${piResponse.code} on ${original.url.encodedPath} — routing to cloud")
            fallbackToCloud(original)
        } else {
            piResponse
        }
    }

    private fun fallbackToCloud(original: Request): Response {
        val cloudUrl = original.url.newBuilder()
            .scheme("https")
            .host(cloudHost)
            .port(443)
            .build()
        return cloudClient.newCall(original.newBuilder().url(cloudUrl).build()).execute()
    }
}

@Module
@InstallIn(SingletonComponent::class)
object AppModule {

    @Provides
    @Singleton
    fun provideOkHttpClient(@ApplicationContext context: Context): OkHttpClient {
        return OkHttpClient.Builder()
            .connectTimeout(2, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .addInterceptor(DynamicPiUrlInterceptor(context))
            .addInterceptor(HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.BODY
            })
            .build()
    }

    @Provides
    @Singleton
    fun provideApiService(client: OkHttpClient): ApiService {
        return Retrofit.Builder()
            .baseUrl(Constants.PI_URL)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ApiService::class.java)
    }

    @Provides
    @Singleton
    fun provideAppDatabase(@ApplicationContext context: Context): AppDatabase {
        return AppDatabase.getDatabase(context)
    }

    @Provides
    @Singleton
    fun provideNetworkUtils(@ApplicationContext context: Context): NetworkUtils {
        return NetworkUtils(context)
    }

    @Provides
    @Singleton
    fun provideApplicationScope(): CoroutineScope {
        return CoroutineScope(SupervisorJob())
    }

    @Provides
    @Singleton
    fun providePaymentManager(
        @ApplicationContext context: Context,
        scope: CoroutineScope,
        db: AppDatabase
    ): PaymentManager {
        return PaymentManager(context, scope, db)
    }

    @Provides
    @Singleton
    fun provideWirePaymentProcessor(paymentManager: PaymentManager): WirePaymentProcessor {
        return WirePaymentProcessor(paymentManager)
    }

    @Provides
    @Singleton
    fun provideScannerManager(scope: CoroutineScope): ScannerManager {
        return ScannerManager(scope)
    }

    @Provides
    @Singleton
    fun provideProductDao(db: AppDatabase): ProductDao = db.productDao()

    @Provides
    @Singleton
    fun provideSaleDao(db: AppDatabase): SaleDao = db.saleDao()

    @Provides
    @Singleton
    fun provideCartDao(db: AppDatabase): CartDao = db.cartDao()

    @Provides
    @Singleton
    fun providePaymentIntentDao(db: AppDatabase): PaymentIntentDao = db.paymentIntentDao()

    @Provides
    @Singleton
    fun provideCustomerDao(db: AppDatabase): CustomerDao = db.customerDao()

    @Provides
    @Singleton
    fun provideConsignmentDao(db: AppDatabase): ConsignmentDao = db.consignmentDao()

    // ── ZettleServerApi — Pi primary, Replit cloud fallback ───────────────────
    // Pi connect timeout is short (3 s) so failover happens quickly.
    // If Pi responds but slowly, the longer read timeout (45 s) applies.
    @Provides
    @Singleton
    fun provideZettleServerApi(@ApplicationContext context: Context): ZettleServerApi {
        val piFirstClient = OkHttpClient.Builder()
            .connectTimeout(3, TimeUnit.SECONDS)
            .readTimeout(45, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .addInterceptor(PiFirstCloudFallbackInterceptor(context))
            .build()
        return Retrofit.Builder()
            .baseUrl(Constants.PI_URL)          // base = Pi; interceptor rewrites & falls back
            .client(piFirstClient)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ZettleServerApi::class.java)
    }
}
