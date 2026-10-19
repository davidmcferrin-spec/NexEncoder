<?php
declare(strict_types=1);

const NEXENCODER_FEED = 'http://127.0.0.1:8788';

function nexencoder_json(int $status, array $payload): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($payload, JSON_UNESCAPED_SLASHES);
    exit;
}

function nexencoder_admin_request(): string
{
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        nexencoder_json(405, ['ok' => false, 'detail' => 'POST required']);
    }
    if (($_SERVER['HTTP_X_REQUESTED_WITH'] ?? '') !== 'NexEncoder') {
        nexencoder_json(403, ['ok' => false, 'detail' => 'Missing action header']);
    }
    $raw = file_get_contents('php://input');
    if ($raw === false) {
        nexencoder_json(400, ['ok' => false, 'detail' => 'request body is unreadable']);
    }
    if (strlen($raw) > 65536) {
        nexencoder_json(400, ['ok' => false, 'detail' => 'request body is too large']);
    }
    return $raw;
}

function nexencoder_pass(int $status, string $type, string $payload): never
{
    if ($status < 100 || $status > 599) {
        $status = 502;
    }
    http_response_code($status);
    header('Content-Type: ' . $type);
    header('Cache-Control: no-store');
    echo $payload;
    exit;
}

/**
 * GET the Python live feed and return [status, content type, body].
 *
 * @return array{0:int,1:string,2:string}
 */
function nexencoder_get(string $path, int $timeout = 8): array
{
    $down = json_encode(['ok' => false, 'detail' => 'The encoder feed is not answering']);
    $url = NEXENCODER_FEED . $path;
    if (!function_exists('curl_init')) {
        return [503, 'application/json; charset=utf-8', $down];
    }
    $ch = curl_init($url);
    if ($ch === false) {
        return [503, 'application/json; charset=utf-8', $down];
    }
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_CONNECTTIMEOUT => 2,
    ]);
    $payload = curl_exec($ch);
    $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $type = (string) curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    $errno = curl_errno($ch);
    curl_close($ch);
    if ($payload === false || $errno !== 0 || $status === 0) {
        return [503, 'application/json; charset=utf-8', $down];
    }
    if ($type === '') {
        $type = 'application/json; charset=utf-8';
    }
    return [$status, $type, $payload];
}

function nexencoder_get_pass(string $path): never
{
    [$status, $type, $payload] = nexencoder_get($path);
    nexencoder_pass($status, $type, $payload);
}

function nexencoder_stream(string $path): never
{
    $url = NEXENCODER_FEED . $path;
    $status = 0;
    $type = 'text/event-stream; charset=utf-8';
    $started = false;
    if (function_exists('apache_setenv')) {
        apache_setenv('no-gzip', '1');
    }
    ini_set('zlib.output_compression', '0');
    ini_set('implicit_flush', '1');
    while (ob_get_level() > 0) {
        ob_end_flush();
    }
    if (!function_exists('curl_init')) {
        nexencoder_json(503, ['ok' => false, 'detail' => 'The encoder feed is not answering']);
    }
    $ch = curl_init($url);
    if ($ch === false) {
        nexencoder_json(503, ['ok' => false, 'detail' => 'The encoder feed is not answering']);
    }
    curl_setopt_array($ch, [
        CURLOPT_HEADERFUNCTION => static function ($ch, string $header) use (&$status, &$type): int {
            if (preg_match('/^HTTP\/\S+\s+(\d{3})/', $header, $match) === 1) {
                $status = (int) $match[1];
            }
            if (stripos($header, 'Content-Type:') === 0) {
                $type = trim(substr($header, 13));
            }
            return strlen($header);
        },
        CURLOPT_WRITEFUNCTION => static function ($ch, string $data) use (&$started, &$status, &$type): int {
            if (!$started) {
                $started = true;
                http_response_code($status > 0 ? $status : 200);
                header('Content-Type: ' . $type);
                header('Cache-Control: no-cache');
                header('X-Accel-Buffering: no');
            }
            echo $data;
            flush();
            return strlen($data);
        },
        CURLOPT_CONNECTTIMEOUT => 2,
        CURLOPT_TIMEOUT => 0,
    ]);
    curl_exec($ch);
    curl_close($ch);
    if (!$started) {
        nexencoder_json(503, ['ok' => false, 'detail' => 'The encoder feed is not answering']);
    }
    exit;
}

function nexencoder_relay(int $status, string $payload): never
{
    if ($status < 100 || $status > 599) {
        $status = 502;
    }
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo $payload;
    exit;
}

/**
 * POST a body to the Python live feed and return [status, body].
 *
 * @param list<string> $headers
 * @return array{0:int,1:string}
 */
function nexencoder_feed(string $path, string $body, array $headers): array
{
    $down = json_encode(['ok' => false, 'detail' => 'The encoder feed is not answering']);
    $url = NEXENCODER_FEED . $path;
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        if ($ch === false) {
            return [503, $down];
        }
        curl_setopt_array($ch, [
            CURLOPT_CUSTOMREQUEST => 'POST',
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 5,
            CURLOPT_CONNECTTIMEOUT => 2,
        ]);
        $payload = curl_exec($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        $errno = curl_errno($ch);
        curl_close($ch);
        if ($payload === false || $errno !== 0 || $status === 0) {
            return [503, $down];
        }
        return [$status, $payload];
    }

    $context = stream_context_create([
        'http' => [
            'method' => 'POST',
            'header' => implode("\r\n", $headers),
            'content' => $body,
            'ignore_errors' => true,
            'timeout' => 5,
        ],
    ]);
    $payload = @file_get_contents($url, false, $context);
    $status = 0;
    if (isset($http_response_header[0]) && preg_match('/\s(\d{3})(?:\s|$)/', $http_response_header[0], $match) === 1) {
        $status = (int) $match[1];
    }
    if ($payload === false || $status === 0) {
        return [503, $down];
    }
    return [$status, $payload];
}
