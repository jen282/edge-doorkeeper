cmake_minimum_required(VERSION 3.16)

# 비밀값(Wi-Fi 비밀번호, 토큰)은 커밋하지 않는 sdkconfig.local에 둔다.
# 기본값은 sdkconfig가 새로 만들어질 때만 적용되므로, 값을 바꾼 뒤에는 sdkconfig를 지우고 다시 빌드한다.
# 카메라 빌드: DOORKEEPER_SOURCE=camera idf.py -B build_camera -D SDKCONFIG=build_camera/sdkconfig build
set(_defaults "sdkconfig.defaults")
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.local")
    list(APPEND _defaults "sdkconfig.local")
endif()
if("$ENV{DOORKEEPER_SOURCE}" STREQUAL "camera")
    list(APPEND _defaults "sdkconfig.camera")
endif()
set(SDKCONFIG_DEFAULTS "${_defaults}")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(edge_doorkeeper)
