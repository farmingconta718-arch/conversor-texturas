[app]
title = Conversor de Texturas AST
package.name = conversorast
package.domain = br.bh.conversor
source.dir = .
source.include_exts = py,png,jpg,jpeg,ttf,so
source.exclude_dirs = tests, bin, .buildozer, __pycache__
version = 1.0
requirements = python3,kivy,pillow,numpy,pyjnius
orientation = portrait
fullscreen = 0
android.permissions = android.permission.READ_EXTERNAL_STORAGE, android.permission.WRITE_EXTERNAL_STORAGE, android.permission.MANAGE_EXTERNAL_STORAGE
android.api = 34
android.minapi = 24
android.ndk_api = 24
android.archs = arm64-v8a
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so
android.extra_manifest_application_arguments = ./manifest_application.xml
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 0
