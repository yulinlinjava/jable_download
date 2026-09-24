package com.example.upload;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.util.UriComponentsBuilder;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

// Spring Boot 3.2+。浏览器只访问本控制器，JABLE_API_KEY 只保存在服务器。
@RestController
@RequestMapping("/upload")
public class UploadProxyController {

    private final RestClient client;
    private final String remoteBaseUrl;

    public UploadProxyController(
            RestClient.Builder builder,
            @Value("${jable-upload.base-url}") String remoteBaseUrl,
            @Value("${jable-upload.api-key}") String apiKey) {
        this.remoteBaseUrl = remoteBaseUrl.replaceFirst("/+$", "");
        this.client = builder.defaultHeader("X-API-Key", apiKey)
                .defaultHeader(HttpHeaders.ACCEPT, MediaType.APPLICATION_JSON_VALUE)
                .build();
    }

    @GetMapping("/chunk")
    public ResponseEntity<String> checkChunks(@RequestParam String identifier) {
        String url = UriComponentsBuilder.fromHttpUrl(remoteBaseUrl + "/upload/chunk")
                .queryParam("identifier", identifier).build().encode().toUriString();
        return relay(client.get().uri(url));
    }

    @PostMapping(value = "/chunk", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<String> uploadChunk(
            @RequestParam String identifier,
            @RequestParam Integer chunkNumber,
            @RequestParam Integer totalChunks,
            @RequestParam MultipartFile file) throws IOException {
        return relayMultipart("/upload/chunk", file, "chunk.part", Map.of(
                "identifier", identifier,
                "chunkNumber", String.valueOf(chunkNumber),
                "totalChunks", String.valueOf(totalChunks)));
    }

    @PostMapping(value = "/file", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<String> uploadFile(@RequestParam MultipartFile file) throws IOException {
        String fileName = safeFileName(file.getOriginalFilename());
        return relayMultipart("/upload/file", file, fileName, Map.of());
    }

    @PostMapping(value = "/merge", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> merge(@RequestBody Map<String, Object> body) {
        // merge 必须包含 identifier 和 fileName；totalChunks 由分片记录读取。
        return relay(client.post().uri(remoteBaseUrl + "/upload/merge")
                .contentType(MediaType.APPLICATION_JSON).body(body));
    }

    @PostMapping(value = "/deleteByPath", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> deleteByPath(@RequestBody List<String> paths) {
        return relay(client.post().uri(remoteBaseUrl + "/upload/deleteByPath")
                .contentType(MediaType.APPLICATION_JSON).body(paths));
    }

    @DeleteMapping("/{identifier}")
    public ResponseEntity<String> deleteByIdentifier(@PathVariable String identifier) {
        return relay(client.delete().uri(remoteBaseUrl + "/upload/{identifier}", identifier));
    }

    private ResponseEntity<String> relayMultipart(
            String path, MultipartFile source, String fileName,
            Map<String, String> fields) throws IOException {
        Path temporary = Files.createTempFile("jable-upload-", ".part");
        try {
            source.transferTo(temporary);
            MultipartBodyBuilder body = new MultipartBodyBuilder();
            fields.forEach(body::part);
            // FileSystemResource 可以重复打开，避免 InputStreamResource 被预读后无法再次读取。
            body.part("file", new FileSystemResource(temporary))
                    .filename(fileName)
                    .contentType(contentType(source));
            MultiValueMap<String, org.springframework.http.HttpEntity<?>> parts = body.build();
            return relay(client.post().uri(remoteBaseUrl + path)
                    .contentType(MediaType.MULTIPART_FORM_DATA).body(parts));
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    private ResponseEntity<String> relay(RestClient.RequestHeadersSpec<?> request) {
        return request.exchange((outgoing, incoming) -> {
            byte[] bytes = incoming.getBody().readAllBytes();
            MediaType type = incoming.getHeaders().getContentType();
            ResponseEntity.BodyBuilder response = ResponseEntity.status(incoming.getStatusCode());
            if (type != null) response.contentType(type);
            return response.body(new String(bytes, StandardCharsets.UTF_8));
        });
    }

    private static MediaType contentType(MultipartFile file) {
        try {
            return file.getContentType() == null
                    ? MediaType.APPLICATION_OCTET_STREAM
                    : MediaType.parseMediaType(file.getContentType());
        } catch (IllegalArgumentException ignored) {
            return MediaType.APPLICATION_OCTET_STREAM;
        }
    }

    private static String safeFileName(String value) {
        String name = value == null ? "file.bin" : value.replace('\\', '/');
        name = name.substring(name.lastIndexOf('/') + 1).trim();
        if (name.isBlank() || name.equals(".") || name.equals("..")) return "file.bin";
        return name.replaceAll("[\\x00-\\x1f\\x7f]", "");
    }
}

/* application.yml
jable-upload:
  base-url: http://127.0.0.1:8000
  api-key: ${JABLE_API_KEY}

# 转发大分片时同步提高 Spring multipart 限制。
spring:
  servlet:
    multipart:
      max-file-size: 50MB
      max-request-size: 52MB
*/
