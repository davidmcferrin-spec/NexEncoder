<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$kind = $_GET['kind'] ?? '';
if ($kind !== 'query' && $kind !== 'query_range' && $kind !== 'names') {
    nexencoder_json(404, ['ok' => false, 'detail' => 'Not found']);
}
$query = $_SERVER['QUERY_STRING'] ?? '';
$query = preg_replace('/(?:^|&)kind=[^&]*/', '', $query);
$query = is_string($query) ? trim($query, '&') : '';
$path = '/api/prom/' . $kind;
if ($query !== '') {
    $path .= '?' . $query;
}
nexencoder_get_pass($path);
