<?php
declare(strict_types=1);

// 示例入口：public/upload.php。
// 支持本项目 /upload/* 标准协议，以及旧 Vue 客户端 /api/bigFileUploads 单接口协议。
// 密钥只从服务器环境变量读取，绝不能返回给浏览器。
function remoteBaseUrl(): string
{
    return rtrim(getenv('JABLE_UPLOAD_BASE_URL') ?: 'http://127.0.0.1:8000', '/');
}

function apiKey(): string
{
    $value = getenv('JABLE_API_KEY');
    if ($value === false || $value === '') fail(500, '服务器未配置 JABLE_API_KEY');
    return $value;
}

/** @return never */
function jsonResponse(int $status, array $payload): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

/** @return never */
function fail(int $status, string $message, mixed $data = null): void
{
    jsonResponse($status, ['code' => $status, 'message' => $message, 'msg' => $message, 'data' => $data]);
}

/** @return array{status:int,contentType:string,body:string} */
function remoteRequest(string $method, string $remotePath, ?array $multipart = null,
                       ?string $jsonBody = null): array
{
    $curl = curl_init(remoteBaseUrl() . $remotePath);
    if ($curl === false) fail(500, '无法初始化 cURL');
    $headers = ['Accept: application/json', 'X-API-Key: ' . apiKey()];
    $options = [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_CONNECTTIMEOUT => 15,
        CURLOPT_TIMEOUT => 7200,
        CURLOPT_HTTPHEADER => $headers,
    ];
    if ($multipart !== null) {
        // 直接传 CURLFile，避免把整个分片再次读入 PHP 内存。
        $options[CURLOPT_POSTFIELDS] = $multipart;
    } elseif ($jsonBody !== null) {
        $headers[] = 'Content-Type: application/json';
        $options[CURLOPT_HTTPHEADER] = $headers;
        $options[CURLOPT_POSTFIELDS] = $jsonBody;
    }
    curl_setopt_array($curl, $options);
    $body = curl_exec($curl);
    if ($body === false) {
        $message = curl_error($curl);
        curl_close($curl);
        fail(502, '上传服务连接失败：' . $message);
    }
    $result = [
        'status' => (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE),
        'contentType' => (string) curl_getinfo($curl, CURLINFO_CONTENT_TYPE),
        'body' => (string) $body,
    ];
    curl_close($curl);
    return $result;
}

/** @return never */
function relay(string $method, string $remotePath, ?array $multipart = null,
               ?string $jsonBody = null): void
{
    $response = remoteRequest($method, $remotePath, $multipart, $jsonBody);
    http_response_code($response['status'] ?: 502);
    header('Content-Type: ' . ($response['contentType'] ?: 'application/json; charset=utf-8'));
    echo $response['body'];
    exit;
}

function input(string $name): string
{
    $value = $_POST[$name] ?? '';
    if (!is_string($value) || $value === '') fail(400, "缺少参数 {$name}");
    return $value;
}

function positiveInteger(string $name): int
{
    $value = input($name);
    if (!ctype_digit($value) || (int) $value < 1) fail(400, "参数 {$name} 必须是正整数");
    return (int) $value;
}

function uploadedFile(string $name, string $remoteName): CURLFile
{
    $file = $_FILES[$name] ?? null;
    if (!is_array($file) || ($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        fail(400, "上传分片 {$name} 无效");
    }
    return new CURLFile((string) $file['tmp_name'],
        (string) ($file['type'] ?: 'application/octet-stream'), $remoteName);
}

/** @return array<string,mixed> */
function decodedRemote(array $response): array
{
    $payload = json_decode($response['body'], true);
    if (!is_array($payload)) fail(502, '上传服务返回了无效 JSON');
    $status = $response['status'] ?: 502;
    if ($status < 200 || $status >= 300 || ($payload['code'] ?? null) !== 0) {
        $message = (string) ($payload['message'] ?? $payload['msg'] ?? "上传服务错误：HTTP {$status}");
        fail($status >= 400 ? $status : 502, $message, $payload['data'] ?? null);
    }
    return is_array($payload['data'] ?? null) ? $payload['data'] : [];
}

function safeFileName(string $value): string
{
    $name = basename(str_replace('\\', '/', trim($value)));
    if ($name === '' || $name === '.' || $name === '..' || strlen($name) > 255) fail(400, 'filename 无效');
    return $name;
}

function uploadScope(): string
{
    // 优先使用项目已有的登录凭据隔离映射；没有凭据时使用 PHP 会话。
    $credential = (string) ($_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['HTTP_X_UPLOAD_USER'] ?? '');
    if ($credential !== '') return hash('sha256', $credential);
    if (session_status() === PHP_SESSION_NONE) session_start();
    return session_id() ?: 'anonymous';
}

function firstChunkSample(): string
{
    $file = $_FILES['chunk'] ?? null;
    if (!is_array($file) || ($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        fail(400, '第一个上传分片无效');
    }
    $handle = fopen((string) $file['tmp_name'], 'rb');
    if ($handle === false) fail(400, '无法读取第一个上传分片');
    $sample = fread($handle, 16 * 1024);
    fclose($handle);
    if ($sample === false || $sample === '') fail(400, '第一个上传分片为空');
    return $sample;
}

function legacyIdentifier(string $fileName, int $totalChunks, int $chunkNumber): string
{
    // 新客户端可以显式提供 identifier；旧客户端不提供时由转发层稳定生成。
    $provided = $_POST['identifier'] ?? '';
    if (is_string($provided) && $provided !== '') {
        if (!preg_match('/^[A-Za-z0-9_-]{1,128}$/', $provided)) fail(400, 'identifier 无效');
        return $provided;
    }
    if (session_status() === PHP_SESSION_NONE) session_start();
    $salt = getenv('JABLE_UPLOAD_ID_SALT') ?: 'jable-upload-proxy';
    $mappingKey = hash('sha256', implode("\n", [
        $salt, uploadScope(), $fileName, (string) $totalChunks,
    ]));
    if ($chunkNumber === 1) {
        $context = hash_init('sha256');
        hash_update($context, $fileName . "\n" . $totalChunks . "\n");
        hash_update($context, firstChunkSample());
        $identifier = substr(hash_final($context), 0, 16);
        $_SESSION['jable_upload_ids'][$mappingKey] = $identifier;
        // 防止长期会话保存过多历史上传映射。
        if (count($_SESSION['jable_upload_ids']) > 100) {
            $_SESSION['jable_upload_ids'] = array_slice(
                $_SESSION['jable_upload_ids'], -100, null, true);
        }
        session_write_close();
        return $identifier;
    }
    $identifier = $_SESSION['jable_upload_ids'][$mappingKey] ?? '';
    session_write_close();
    if (!is_string($identifier) || $identifier === '') {
        fail(409, '上传会话已失效，请从第一个分片重新上传');
    }
    return $identifier;
}

/** @return never */
function legacyBigFileUpload(): void
{
    $fileName = safeFileName(input('filename'));
    $chunkNumber = positiveInteger('chunkNumber');
    $totalChunks = positiveInteger('totalChunks');
    if ($chunkNumber > $totalChunks) fail(400, 'chunkNumber 不能大于 totalChunks');
    $identifier = legacyIdentifier($fileName, $totalChunks, $chunkNumber);
    $multipart = [
        'identifier' => $identifier,
        'chunkNumber' => (string) $chunkNumber,
        'totalChunks' => (string) $totalChunks,
        // 旧客户端字段名为 chunk，本项目上游字段名为 file。
        'file' => uploadedFile('chunk', 'chunk.part'),
    ];
    if (isset($_POST['r2SyncEnabled'])) {
        $multipart['r2SyncEnabled'] = filter_var(
            $_POST['r2SyncEnabled'], FILTER_VALIDATE_BOOLEAN) ? 'true' : 'false';
    }
    $chunk = decodedRemote(remoteRequest('POST', '/upload/chunk', $multipart));
    if ($chunkNumber < $totalChunks) {
        jsonResponse(200, [
            'code' => 200, 'msg' => '分片上传成功', 'message' => '分片上传成功',
            'data' => [
                'url' => '', 'identifier' => $identifier,
                'chunkNumber' => $chunkNumber, 'totalChunks' => $totalChunks,
                'uploadedCount' => $chunk['uploadedCount'] ?? $chunkNumber,
            ],
        ]);
    }
    // 最后一个分片上传成功后，自动调用本项目异步合并接口。
    $mergeBody = ['identifier' => $identifier, 'fileName' => $fileName];
    if (isset($_POST['r2SyncEnabled'])) {
        $mergeBody['r2SyncEnabled'] = filter_var(
            $_POST['r2SyncEnabled'], FILTER_VALIDATE_BOOLEAN);
    }
    $merged = decodedRemote(remoteRequest(
        'POST', '/upload/merge', null,
        json_encode($mergeBody, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)));
    $merged['url'] = (string) ($merged['path'] ?? $merged['hlsPath'] ?? '');
    jsonResponse(200, [
        'code' => 200, 'msg' => '上传完成，后台正在处理文件',
        'message' => '上传完成，后台正在处理文件', 'data' => $merged,
    ]);
}

$method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';

// 兼容用户现有 Vue：chunk、filename、chunked、chunkNumber、totalChunks。
if ($method === 'POST' && $path === '/api/bigFileUploads') legacyBigFileUpload();

if ($method === 'GET' && $path === '/upload/chunk') {
    $identifier = $_GET['identifier'] ?? '';
    if (!is_string($identifier) || $identifier === '') fail(400, '缺少 identifier');
    relay('GET', '/upload/chunk?identifier=' . rawurlencode($identifier));
}

if ($method === 'POST' && $path === '/upload/chunk') {
    relay('POST', '/upload/chunk', [
        'identifier' => input('identifier'),
        'chunkNumber' => input('chunkNumber'),
        'totalChunks' => input('totalChunks'),
        'file' => uploadedFile('file', 'chunk.part'),
    ]);
}

if ($method === 'POST' && $path === '/upload/file') {
    $name = safeFileName((string) ($_FILES['file']['name'] ?? 'file.bin'));
    relay('POST', '/upload/file', ['file' => uploadedFile('file', $name)]);
}

if ($method === 'POST' && in_array($path, ['/upload/merge', '/upload/deleteByPath'], true)) {
    $body = file_get_contents('php://input');
    if ($body === false || json_decode($body, true) === null) fail(400, '请求体必须是有效 JSON');
    relay('POST', $path, null, $body);
}

if ($method === 'DELETE' && preg_match('#^/upload/([A-Za-z0-9_-]{1,128})$#', $path, $match)) {
    relay('DELETE', '/upload/' . rawurlencode($match[1]));
}

fail(404, '转发接口不存在');


