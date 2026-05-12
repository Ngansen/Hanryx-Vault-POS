package com.hanryxvault.pos

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.TypeConverters

@Database(
    entities = [
        SaleEntity::class,
        ProductEntity::class,
        ProductSearchEntity::class,
        CartItemEntity::class,
        PaymentIntentEntity::class,
        ProductOutboxEntity::class,
        CustomerEntity::class,
        WantlistItemEntity::class,
        ConsignorEntity::class,
        ConsignmentItemEntity::class,
        SignedTradeInEntity::class
    ],
    version = 10,
    exportSchema = false
)
@TypeConverters(SaleConverters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun saleDao(): SaleDao
    abstract fun productDao(): ProductDao
    abstract fun cartDao(): CartDao
    abstract fun paymentIntentDao(): PaymentIntentDao
    abstract fun customerDao(): CustomerDao
    abstract fun consignmentDao(): ConsignmentDao
    abstract fun signedTradeInDao(): SignedTradeInDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getDatabase(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "vault_pos_database"
                )
                .fallbackToDestructiveMigration()
                .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
