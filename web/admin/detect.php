<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$raw = nexencoder_admin_request();
$decoded = json_decode($raw === '' ? '{}' : $raw, true);
if (!is_array($decoded)) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'detect body must be an object']);
}
$channel = $decoded['channel'] ?? '';
if ($channel !== 'A' && $channel !== 'B') {
    nexencoder_json(400, ['ok' => false, 'detail' => 'channel must be A or B']);
}
[$status, $payload] = nexencoder_feed('/api/channels/' . $channel . '/detect', '{}', [
    'Content-Type: application/json',
    'X-Requested-With: NexEncoder',
    'Content-Length: 2',
]);
nexencoder_relay($status, $payload);
