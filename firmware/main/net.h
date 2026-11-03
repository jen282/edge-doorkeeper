// Wi-Fi STA 연결. 끊기면 계속 재연결한다. SSID·IP는 로그에 남기지 않는다.
#pragma once

#include <stdbool.h>

bool net_start(int timeout_ms);
bool net_connected(void);
int net_rssi(void);
