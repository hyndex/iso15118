import os
from typing import Optional

import environs


class SettingKey:
    PKI_PATH = "PKI_PATH"
    MESSAGE_LOG_JSON = "MESSAGE_LOG_JSON"
    MESSAGE_LOG_EXI = "MESSAGE_LOG_EXI"
    ENABLE_TLS_1_3 = "ENABLE_TLS_1_3"
    EXI_SESSION_DIR = "EXI_SESSION_DIR"
    MESSAGE_LOG_PER_SESSION = "MESSAGE_LOG_PER_SESSION"


shared_settings = {}
SHARED_CWD = os.path.dirname(os.path.abspath(__file__))
JAR_FILE_PATH = SHARED_CWD + "/EXICodec.jar"

WORK_DIR = os.getcwd()


def load_shared_settings(env_path: Optional[str] = None):
    env = environs.Env(eager=False)
    env.read_env(path=env_path)  # read .env file, if it exists

    settings = {
        SettingKey.PKI_PATH: env.str("PKI_PATH", default=SHARED_CWD + "/pki/"),
        # Default off to reduce per-message overhead unless explicitly enabled
        SettingKey.MESSAGE_LOG_JSON: env.bool("MESSAGE_LOG_JSON", default=False),
        SettingKey.MESSAGE_LOG_EXI: env.bool("MESSAGE_LOG_EXI", default=False),
        SettingKey.ENABLE_TLS_1_3: env.bool("ENABLE_TLS_1_3", default=False),
        SettingKey.EXI_SESSION_DIR: env.str("EXI_SESSION_DIR", default=os.path.join(WORK_DIR, "exi_sessions")),
        SettingKey.MESSAGE_LOG_PER_SESSION: env.bool("MESSAGE_LOG_PER_SESSION", default=False),
    }
    shared_settings.update(settings)
    env.seal()  # raise all errors at once, if any
