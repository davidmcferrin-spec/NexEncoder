<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$body = nexencoder_admin_request();
$channel = $_GET['channel'] ?? '';
if (!is_string($channel) || ($channel !== 'A' && $channel !== 'B')) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'channel must be A or B']);
}
[$status, $payload] = nexencoder_feed('/api/channels/' . $channel . '/captions', $body, [
    'Content-Type: application/json',
    'X-Requested-With: NexEncoder',
    'Content-Length: ' . strlen($body),
]);
nexencoder_relay($status, $payload);
