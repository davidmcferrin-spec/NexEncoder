<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

nexencoder_admin_request();
$unit = $_GET['unit'] ?? '';
if (is_string($unit) && $unit !== '') {
    if (strlen($unit) > 200 || str_contains($unit, "\0")) {
        nexencoder_json(400, ['ok' => false, 'detail' => 'unit is not valid']);
    }
    $path = '/api/services/' . rawurlencode($unit) . '/log/clear';
    $timeout = 5;
} else {
    $path = '/api/journal/clear';
    $timeout = 150;
}
[$status, $payload] = nexencoder_feed($path, '', [
    'X-Requested-With: NexEncoder',
    'Content-Length: 0',
], $timeout);
nexencoder_relay($status, $payload);
