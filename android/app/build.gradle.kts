plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.boox.bridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.boox.bridge"
        // Boox Tab X ships Android 11 (API 30). 28 gives a safety margin.
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    packaging {
        jniLibs {
            pickFirsts += setOf("lib/**/libc++_shared.so")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")

    // Onyx Pen SDK — low-latency raw pen capture on Boox devices.
    // Version pinned to a known-good release; check
    //   https://mvnrepository.com/artifact/com.onyx.android.sdk/onyxsdk-pen
    // for newer versions if needed.
    implementation("com.onyx.android.sdk:onyxsdk-pen:1.5.2")
    implementation("com.onyx.android.sdk:onyxsdk-device:1.3.3")

    // WebSocket client
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
}
