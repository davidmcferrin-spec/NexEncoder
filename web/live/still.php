<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$channel = $_GET['channel'] ?? '';
if ($channel !== 'A' && $channel !== 'B') {
    nexencoder_json(404, ['ok' => false, 'detail' => 'Not found']);
}
nexencoder_get_pass('/api/channels/' . $channel . '/still');
