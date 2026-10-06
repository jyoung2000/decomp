; Rebuild Studio NSIS hooks (wired by bundle.windows.nsis.installerHooks in tauri.conf.json).
;
; * Before install/update/uninstall: stop a controller sidecar left running from this install folder
;   (normally the shell's kill-on-close job object already did; this covers a crashed shell).
; * Uninstall keeps projects, settings, tools and stored AI keys unless the user ticks
;   "Delete the application data". Then it also removes %LOCALAPPDATA%\RebuildStudio (store, evidence,
;   downloaded tools, logs) and every "RebuildStudio:*" entry in Windows Credential Manager.
;   Output folders the user chose for rebuilt apps are never touched.

!macro RS_STOP_CONTROLLER
  nsExec::Exec '"$SYSDIR\taskkill.exe" /F /T /FI "IMAGENAME eq rebuild-controller.exe"'
  Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro RS_STOP_CONTROLLER
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro RS_STOP_CONTROLLER
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    ; Must run before the binaries are deleted.
    nsExec::Exec '"$INSTDIR\${MAINBINARYNAME}.exe" --remove-stored-credentials'
    Pop $0
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    RmDir /r "$LOCALAPPDATA\RebuildStudio"
  ${EndIf}
!macroend
