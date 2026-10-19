<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

nexencoder_admin_request();
set_time_limit(180);
[$status, $type, $payload, $headers] = nexencoder_post_download('/api/journal/clear', 150);
if ($status !== 200 || !str_contains($type, 'zip')) {
    nexencoder_pass($status, $type !== '' ? $type : 'application/json; charset=utf-8', $payload);
}
$disposition = $headers['content-disposition'] ?? '';
if (preg_match('/filename="([A-Za-z0-9._-]+)"/', $disposition, $match) === 1) {
    $name = $match[1];
} else {
    $name = 'nexencoder-support-' . gmdate('Ymd\THis\Z') . '-all.zip';
}
$clear = $headers['x-journal-clear'] ?? '';
if ($clear !== 'cleared' && $clear !== 'failed') {
    $clear = 'failed';
}
$detail = preg_replace('/[^\x20-\x7E]/', '', $headers['x-journal-clear-detail'] ?? '') ?? '';
$detail = substr($detail, 0, 180);
header('Content-Type: application/zip');
header('Content-Disposition: attachment; filename="' . $name . '"');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header('X-Journal-Clear: ' . $clear);
if ($detail !== '') {
    header('X-Journal-Clear-Detail: ' . $detail);
}
echo $payload;
