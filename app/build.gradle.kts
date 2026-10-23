plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "com.quique.nmeasim"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.quique.nmeasim"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        ndk {
            // Poco X7 Pro y casi todos los móviles actuales son arm64
            abiFilters += listOf("arm64-v8a")
        }
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
}

chaquopy {
    defaultConfig {
        version = "3.12"
    }
}
