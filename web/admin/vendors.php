<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET') {
    nexencoder_json(405, ['ok' => false, 'detail' => 'GET required']);
}
nexencoder_get_pass('/api/vendors');
