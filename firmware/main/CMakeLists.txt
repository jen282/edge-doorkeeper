idf_component_register(
    SRCS "main.c" "frame_source.c" "model_runner.cc" "net.c" "uplink.c"
    INCLUDE_DIRS "."
    # CI 게이트를 통과한 바로 그 모델 파일을 넣는다 (docs/09-ci.md)
    EMBED_FILES "${CMAKE_CURRENT_SOURCE_DIR}/../../ml/model.tflite"
    REQUIRES postproc esp_partition esp_timer nvs_flash esp_wifi esp_netif esp_event esp_http_client
)
