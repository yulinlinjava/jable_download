package com.cloud.admin.upload;

import com.cloud.common.domain.R;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.yulinlin.data.core.http.HttpFile;
import com.yulinlin.data.core.http.HttpRequest;
import com.yulinlin.data.core.http.HttpRequestClient;
import com.yulinlin.data.core.http.HttpRequestException;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 上传接口适配服务。
 *
 * <p>本服务不再在当前 Java 应用中保存分片、合并文件或调用 FFmpeg，所有操作都转发给
 * JableTVDownload 的 /upload 接口。Controller 的公开方法签名保持不变。</p>
 */
@Service
public class ChunkUploadService {

    private static final Logger log = LoggerFactory.getLogger(ChunkUploadService.class);
    private static final String API_KEY_HEADER = "X-API-Key";
    private static final int MAX_CHUNKS = 100_000;

    private final HttpRequestClient httpRequestClient;
    private final ObjectMapper objectMapper;

    @Value("${remote-upload.base-url}")
    private String baseUrl;

    @Value("${remote-upload.api-key}")
    private String apiKey;

    @Value("${remote-upload.request-timeout-minutes:120}")
    private int requestTimeoutMinutes;

    public ChunkUploadService(HttpRequestClient httpRequestClient, ObjectMapper objectMapper) {
        this.httpRequestClient = httpRequestClient;
        this.objectMapper = objectMapper;
    }

    @PostConstruct
    void validateConfiguration() {
        baseUrl = trimTrailingSlash(baseUrl);
        URI uri = URI.create(baseUrl);
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                || uri.getHost() == null) {
            throw new IllegalStateException("remote-upload.base-url 必须是有效的 HTTP/HTTPS 地址");
        }
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalStateException("remote-upload.api-key 不能为空");
        }
        if (requestTimeoutMinutes < 1) {
            throw new IllegalStateException("remote-upload.request-timeout-minutes 必须大于0");
        }
    }

    public String deleteByPath(List<String> urls) {
        if (urls == null || urls.isEmpty()) return "删除：0个资源";
        try {
            JsonNode data = postJson("/upload/deleteByPath", urls);
            return "删除：" + arraySize(data, "deleted") + "个资源";
        } catch (Exception e) {
            log.error("远程资源删除失败", e);
            return "删除失败：" + safeMessage(e);
        }
    }

    /**
     * 保持旧 H5 所需的 skip、url、uploaded 字段，同时透传远程任务状态。
     */
    public Map<String, Object> checkChunks(String identifier) {
        Map<String, Object> result = new HashMap<>();
        if (!isValidIdentifier(identifier)) {
            result.put("skip", false);
            result.put("error", "identifier非法");
            return result;
        }
        try {
            JsonNode data = send(get("/upload/chunk").query("identifier", identifier));
            boolean completed = data.path("completed").asBoolean(false);
            result.put("skip", completed);
            result.put("completed", completed);
            result.put("uploaded", objectMapper.convertValue(
                    data.path("uploadedChunks"), objectMapper.getTypeFactory()
                            .constructCollectionType(List.class, Integer.class)));
            copyText(data, result, "path", "url");
            copyText(data, result, "path", "path");
            copyText(data, result, "hlsPath", "hlsPath");
            copyText(data, result, "status", "status");
            copyText(data, result, "processingStage", "processingStage");
            copyText(data, result, "message", "message");
            copyText(data, result, "error", "error");
        } catch (Exception e) {
            log.error("查询远程分片失败 identifier={}", identifier, e);
            result.put("skip", false);
            result.put("error", safeMessage(e));
        }
        return result;
    }

    public R<String> uploadFile(MultipartFile file) {
        if (file == null || file.isEmpty()) return R.newInstance("文件为空");
        try (TemporaryUpload temporary = TemporaryUpload.create(
                file, safeFileName(file.getOriginalFilename()), mediaType(file))) {
            JsonNode data = send(post("/upload/file").file("file", temporary.httpFile()));
            return R.newInstance(resourceUrl(data));
        } catch (Exception e) {
            log.error("调用远程完整文件上传失败", e);
            return R.newInstance("上传异常: " + safeMessage(e));
        }
    }

    public String uploadChunk(String identifier, Integer chunkNumber, Integer totalChunks,
                              MultipartFile file) throws IOException {
        if (!isValidIdentifier(identifier)) return "identifier非法";
        if (chunkNumber == null || chunkNumber < 1 || chunkNumber > MAX_CHUNKS) {
            return "chunkNumber非法，分片编号必须从1开始";
        }
        if (totalChunks == null || totalChunks < 1 || totalChunks > MAX_CHUNKS
                || chunkNumber > totalChunks) {
            return "totalChunks非法";
        }
        if (file == null || file.isEmpty()) return "分片为空";

        try (TemporaryUpload temporary = TemporaryUpload.create(
                file, "chunk.part", mediaType(file))) {
            send(post("/upload/chunk")
                    .multipart("identifier", identifier)
                    .multipart("chunkNumber", chunkNumber)
                    .multipart("totalChunks", totalChunks)
                    .file("file", temporary.httpFile()));
        }
        return "success";
    }

    /**
     * 远程 merge 是后台异步处理，成功提交后立即返回可预测的 index.m3u8 地址。
     */
    public String merge(Map<String, String> params) throws IOException {
        if (params == null) return "参数不能为空";
        String identifier = params.get("identifier");
        if (!isValidIdentifier(identifier)) return "identifier非法";
        final String fileName;
        try {
            fileName = safeFileName(params.get("fileName"));
        } catch (IllegalArgumentException e) {
            return "fileName非法";
        }
        JsonNode data = postJson("/upload/merge", Map.of(
                "identifier", identifier,
                "fileName", fileName));
        return resourceUrl(data);
    }

    private JsonNode postJson(String path, Object body) throws IOException {
        return send(post(path).json(body));
    }

    private JsonNode send(HttpRequest request) throws IOException {
        try {
            JsonNode root = request.execute().bodyAs(JsonNode.class);
            int code = root.path("code").asInt(0);
            if (code != 0) {
                throw new IOException(root.path("message").asText("远程上传接口调用失败")
                        + "（code=" + code + "）");
            }
            return root.path("data");
        } catch (HttpRequestException e) {
            throw remoteException(e);
        }
    }

    private HttpRequest get(String path) {
        return configure(httpRequestClient.get(baseUrl + path));
    }

    private HttpRequest post(String path) {
        return configure(httpRequestClient.post(baseUrl + path));
    }

    private HttpRequest configure(HttpRequest request) {
        return request.timeout(Duration.ofMinutes(requestTimeoutMinutes))
                .header(API_KEY_HEADER, apiKey)
                .header("Accept", "application/json");
    }

    private static MediaType mediaType(MultipartFile file) {
        String contentType = file.getContentType();
        if (contentType == null || contentType.isBlank()) {
            return MediaType.APPLICATION_OCTET_STREAM;
        }
        try {
            return MediaType.parseMediaType(contentType);
        } catch (IllegalArgumentException ignored) {
            return MediaType.APPLICATION_OCTET_STREAM;
        }
    }

    private IOException remoteException(HttpRequestException exception) {
        Integer status = exception.getStatusCode();
        String message = "远程上传接口调用失败";
        int code = status == null ? 0 : status;
        byte[] responseBody = exception.getResponseBody();
        if (responseBody.length > 0) {
            try {
                JsonNode root = objectMapper.readTree(responseBody);
                message = root.path("message").asText(message);
                code = root.path("code").asInt(code);
            } catch (Exception ignored) {
                // 非 JSON 错误响应只返回状态码，不把完整响应写入日志或异常。
            }
        }
        String statusText = status == null ? "网络请求失败" : "HTTP " + status;
        return new IOException(message + "（" + statusText + "，code=" + code + "）", exception);
    }

    private String resourceUrl(JsonNode data) throws IOException {
        for (String field : List.of("hlsPath", "path", "previewPath")) {
            if (data.hasNonNull(field) && !data.path(field).asText().isBlank()) {
                return data.path(field).asText();
            }
        }
        throw new IOException("上传接口未返回资源地址");
    }

    private static void copyText(JsonNode source, Map<String, Object> target,
                                 String sourceField, String targetField) {
        if (source.hasNonNull(sourceField)) target.put(targetField, source.path(sourceField).asText());
    }

    private static int arraySize(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isArray() ? value.size() : value.asInt(0);
    }

    private static String safeFileName(String originalName) {
        if (originalName == null || originalName.isBlank()) {
            throw new IllegalArgumentException("文件名不能为空");
        }
        String normalized = originalName.replace('\\', '/');
        String name = normalized.substring(normalized.lastIndexOf('/') + 1).trim()
                .replaceAll("[\\x00-\\x1F\\x7F]", "");
        if (name.isBlank() || ".".equals(name) || "..".equals(name)) {
            throw new IllegalArgumentException("非法文件名");
        }
        return name;
    }

    private static boolean isValidIdentifier(String identifier) {
        return identifier != null && identifier.matches("[a-zA-Z0-9_-]{1,128}");
    }

    private static String safeMessage(Exception exception) {
        String message = exception.getMessage();
        return message == null || message.isBlank() ? exception.getClass().getSimpleName() : message;
    }

    private static String trimTrailingSlash(String value) {
        if (value == null) return "";
        return value.trim().replaceFirst("/+$", "");
    }

    /**
     * Spring multipart 在发送前可能读取 Resource 获取长度。使用临时文件可重复打开输入流，
     * 避免 InputStreamResource 被提前消费，同时不会把大文件整体放入 Java 堆。
     */
    private static final class TemporaryUpload implements AutoCloseable {
        private final Path directory;
        private final Path file;
        private final MediaType mediaType;

        private TemporaryUpload(Path directory, Path file, MediaType mediaType) {
            this.directory = directory;
            this.file = file;
            this.mediaType = mediaType;
        }

        private static TemporaryUpload create(
                MultipartFile source, String fileName, MediaType mediaType) throws IOException {
            Path directory = Files.createTempDirectory("jable-upload-");
            Path target = directory.resolve(fileName);
            try {
                source.transferTo(target);
                return new TemporaryUpload(directory, target, mediaType);
            } catch (Exception exception) {
                Files.deleteIfExists(target);
                Files.deleteIfExists(directory);
                if (exception instanceof IOException ioException) throw ioException;
                throw new IOException("创建上传临时文件失败", exception);
            }
        }

        private HttpFile httpFile() {
            return HttpFile.of(file, mediaType);
        }

        @Override
        public void close() {
            try {
                Files.deleteIfExists(file);
                Files.deleteIfExists(directory);
            } catch (IOException exception) {
                log.warn("清理上传临时文件失败 path={}", file, exception);
            }
        }
    }

}
