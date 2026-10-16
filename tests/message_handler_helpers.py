"""実際のメッセージ処理へテスト用サービスと外部境界のモックを渡す。"""

from dataclasses import fields
from unittest.mock import Mock, patch

from persona_bot.core import message_handler
from persona_bot.core.message_handler import MessageHandler


def make_message_handler(dependencies, test_case):
    service_names = {field.name for field in fields(MessageHandler)}
    handler = MessageHandler(**{
        name: dependencies.get(name, Mock(name=name)) for name in service_names
    })
    for name, replacement in dependencies.items():
        if name not in service_names and hasattr(message_handler, name):
            patcher = patch.object(message_handler, name, replacement)
            patcher.start()
            test_case.addCleanup(patcher.stop)
    return handler
