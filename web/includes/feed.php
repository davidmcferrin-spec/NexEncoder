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
