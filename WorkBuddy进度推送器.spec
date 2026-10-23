# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


a = Analysis(['wb2ntfy.py'],
             pathex=['C:\\Users\\Administrator\\WorkBuddy\\2026-10-22-19-20-05\\wb2ntfy'],
             binaries=[],
             datas=[('app.gif', '.'), ('app.ico', '.')],
             hiddenimports=[],
             hookspath=[],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='WorkBuddy进度推送器',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=False , icon='app.ico')
