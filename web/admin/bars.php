<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$raw = nexencoder_admin_request();
$decoded = json_decode($raw, true);
if (!is_array($decoded)) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'bars body must be an object']);
}
$channel = $decoded['channel'] ?? '';
if ($channel !== 'A' && $channel !== 'B') {
    nexencoder_json(400, ['ok' => false, 'detail' => 'channel must be A or B']);
}
unset($decoded['channel']);
$body = json_encode($decoded);
if ($body === false) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'bars body must be an object']);
}
[$status, $payload] = nexencoder_feed('/api/channels/' . $channel . '/bars', $body, [
    'Content-Type: application/json',
    'X-Requested-With: NexEncoder',
    'Content-Length: ' . strlen($body),
]);
nexencoder_relay($status, $payload);
