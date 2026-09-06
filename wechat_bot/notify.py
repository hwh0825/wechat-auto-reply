# -*- coding: utf-8 -*-
"""Windows 弹窗提醒：敏感消息/熔断时通知用户本人（无第三方依赖）

内容通过环境变量传给 PowerShell，用 .NET 的 XmlElement.InnerText 写入文本，
由系统自动转义 XML 特殊字符，避免联系人名/消息里出现 <、&、单引号等导致
toast XML 解析失败或注入。
"""
import logging
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000   # 无控制台模式下调 PowerShell 不许闪黑框

_PS_TEMPLATE = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$root = $xml.CreateElement('toast')
$vis = $xml.CreateElement('visual')
$bind = $xml.CreateElement('binding')
$bind.SetAttribute('template', 'ToastGeneric')
$t1 = $xml.CreateElement('text'); $t1.InnerText = $env:DSH_NOTIFY_TITLE
$t2 = $xml.CreateElement('text'); $t2.InnerText = $env:DSH_NOTIFY_MSG
$bind.AppendChild($t1) | Out-Null
$bind.AppendChild($t2) | Out-Null
$vis.AppendChild($bind) | Out-Null
$root.AppendChild($vis) | Out-Null
$xml.AppendChild($root) | Out-Null
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast)
'''


def notify(title: str, message: str):
    try:
        env = os.environ.copy()
        env["DSH_NOTIFY_TITLE"] = title
        env["DSH_NOTIFY_MSG"] = message
        subprocess.run(["powershell", "-NoProfile", "-Command", _PS_TEMPLATE],
                       capture_output=True, timeout=15, env=env,
                       creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        logging.warning("系统通知发送失败: %s", e)
