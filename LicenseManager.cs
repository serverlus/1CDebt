/*
 * LicenseManager.cs
 * =================
 * Управление лицензиями:
 *   — Хранение токена в реестре Windows (зашифровано DPAPI)
 *   — Онлайн-активация через твой License Server
 *   — Локальная валидация (работает offline 72 часа)
 *   — Привязка к ИНН + MAC-адресу
 */

using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using Microsoft.Win32;
using Newtonsoft.Json;

namespace DebtScoring
{
    public class ActivationResult
    {
        public bool   Success    { get; set; }
        public string Status     { get; set; } = "";
        public string Info       { get; set; } = "";
        public string? Token     { get; set; }
        public string? ExpiresAt { get; set; }
        public string? Plan      { get; set; }
        public string? Error     { get; set; }
    }

    public static class LicenseManager
    {
        // ── Константы ──────────────────────────────────────────────────────

        // URL твоего сервера лицензий (FastAPI)
        private const string LICENSE_SERVER_URL = "https://license.твойдомен.ru/api/v1";

        // Ключ реестра где храним токен (шифруется DPAPI — только текущий пользователь может читать)
        private const string REGISTRY_KEY  = @"SOFTWARE\DebtScoring";
        private const string REGISTRY_TOKEN    = "LicenseToken";
        private const string REGISTRY_EXPIRES  = "ExpiresAt";
        private const string REGISTRY_LAST_CHECK = "LastCheck";
        private const string REGISTRY_PLAN     = "Plan";

        // Оффлайн grace-период: 72 часа
        private static readonly TimeSpan GRACE_PERIOD = TimeSpan.FromHours(72);

        // HTTP клиент (singleton)
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(10)
        };

        // ── Публичный API ───────────────────────────────────────────────────

        /// <summary>
        /// Локальная проверка лицензии.
        /// Читает токен из реестра, проверяет срок действия.
        /// Если онлайн-проверка не проводилась >24 часов — идёт на сервер.
        /// </summary>
        public static bool CheckLocal()
        {
            try
            {
                string? token      = GetStoredToken();
                string? expiresStr = GetRegistryValue(REGISTRY_EXPIRES);
                string? lastCheck  = GetRegistryValue(REGISTRY_LAST_CHECK);

                if (string.IsNullOrEmpty(token) || string.IsNullOrEmpty(expiresStr))
                    return false;

                // Проверяем срок действия
                if (!DateTime.TryParse(expiresStr, out DateTime expires))
                    return false;

                if (DateTime.UtcNow > expires)
                    return false;  // Подписка истекла

                // Проверяем когда последний раз валидировались онлайн
                bool needOnlineCheck = true;
                if (DateTime.TryParse(lastCheck, out DateTime lastCheckTime))
                    needOnlineCheck = DateTime.UtcNow - lastCheckTime > TimeSpan.FromHours(24);

                if (needOnlineCheck)
                {
                    // Пытаемся провалидировать онлайн
                    bool onlineResult = ValidateOnline(token).GetAwaiter().GetResult();
                    if (onlineResult)
                    {
                        SaveRegistryValue(REGISTRY_LAST_CHECK,
                            DateTime.UtcNow.ToString("o"));
                        return true;
                    }

                    // Онлайн недоступен — работаем в grace-период
                    if (DateTime.TryParse(lastCheck, out DateTime lastOk))
                    {
                        bool inGrace = DateTime.UtcNow - lastOk < GRACE_PERIOD;
                        return inGrace;
                    }

                    return false;  // Никогда не валидировались онлайн
                }

                return true;  // Всё ок, онлайн-проверка не нужна
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Онлайн-активация: отправляет ключ на сервер, получает токен,
        /// сохраняет в реестр.
        /// </summary>
        public static ActivationResult ActivateOnline(
            string licenseKey, string inn, string? macAddress = null)
        {
            try
            {
                macAddress ??= GetMacAddress();

                var payload = JsonConvert.SerializeObject(new
                {
                    license_key = licenseKey.Trim().ToUpper(),
                    inn         = inn.Trim(),
                    mac_address = macAddress,
                    hostname    = Environment.MachineName,
                    os_version  = Environment.OSVersion.ToString()
                });

                var content  = new StringContent(payload, Encoding.UTF8, "application/json");
                var response = _http.PostAsync($"{LICENSE_SERVER_URL}/activate", content)
                                    .GetAwaiter().GetResult();

                string responseBody = response.Content.ReadAsStringAsync()
                                               .GetAwaiter().GetResult();

                dynamic? result = JsonConvert.DeserializeObject(responseBody);

                if (result == null)
                    return Fail("Пустой ответ сервера");

                string status = (string?)result.status ?? "";

                if (status == "activated" || status == "already_activated")
                {
                    string token     = (string)result.device_secret;
                    string expiresAt = (string)result.expires_at;
                    string plan      = (string)result.plan;

                    // Сохраняем в реестр (DPAPI-шифрование)
                    SaveTokenSecure(token);
                    SaveRegistryValue(REGISTRY_EXPIRES,    expiresAt);
                    SaveRegistryValue(REGISTRY_LAST_CHECK, DateTime.UtcNow.ToString("o"));
                    SaveRegistryValue(REGISTRY_PLAN,       plan);

                    return new ActivationResult
                    {
                        Success   = true,
                        Status    = "activated",
                        Token     = token,
                        ExpiresAt = expiresAt,
                        Plan      = plan,
                        Info      = $"Активировано. Тариф: {plan}. Действует до: {expiresAt}"
                    };
                }

                string errorMsg = (string?)result.message
                    ?? (string?)result.detail
                    ?? "Неизвестная ошибка сервера";

                return Fail(errorMsg);
            }
            catch (HttpRequestException)
            {
                return Fail("Сервер лицензий недоступен. Проверьте подключение к интернету.");
            }
            catch (Exception ex)
            {
                return Fail($"Ошибка активации: {ex.Message}");
            }
        }

        /// <summary>
        /// Возвращает сохранённый токен из реестра (расшифрованный).
        /// Используется в DebtScoringAddIn для генерации ключа шифрования Python-кода.
        /// </summary>
        public static string? GetStoredToken()
        {
            try
            {
                using var key = Registry.CurrentUser.OpenSubKey(REGISTRY_KEY);
                byte[]? encrypted = key?.GetValue(REGISTRY_TOKEN) as byte[];
                if (encrypted == null) return null;

                // Расшифровываем через DPAPI (только текущий пользователь может читать)
                byte[] plain = ProtectedData.Unprotect(
                    encrypted, null, DataProtectionScope.CurrentUser);
                return Encoding.UTF8.GetString(plain);
            }
            catch
            {
                return null;
            }
        }

        // ── Приватные методы ───────────────────────────────────────────────

        private static async Task<bool> ValidateOnline(string token)
        {
            try
            {
                string inn        = GetStoredInn() ?? "";
                string macAddress = GetMacAddress();

                // HMAC от сегодняшней даты — защита от replay-атак
                string today     = DateTime.UtcNow.ToString("yyyy-MM-dd");
                string checksum  = ComputeHmac(token, $"{inn}:{macAddress}:{today}")[..16];

                var payload = JsonConvert.SerializeObject(new
                {
                    token, inn, mac_address = macAddress, checksum
                });

                var content  = new StringContent(payload, Encoding.UTF8, "application/json");
                var response = await _http.PostAsync($"{LICENSE_SERVER_URL}/validate", content);

                string body   = await response.Content.ReadAsStringAsync();
                dynamic? result = JsonConvert.DeserializeObject(body);

                return result?.valid == true;
            }
            catch
            {
                return false;  // Нет интернета — вернёмся к grace-периоду
            }
        }

        private static void SaveTokenSecure(string token)
        {
            // Шифруем через Windows DPAPI — привязка к текущему пользователю
            byte[] plain     = Encoding.UTF8.GetBytes(token);
            byte[] encrypted = ProtectedData.Protect(
                plain, null, DataProtectionScope.CurrentUser);

            using var key = Registry.CurrentUser.CreateSubKey(REGISTRY_KEY);
            key.SetValue(REGISTRY_TOKEN, encrypted, RegistryValueKind.Binary);
        }

        private static void SaveRegistryValue(string name, string value)
        {
            using var key = Registry.CurrentUser.CreateSubKey(REGISTRY_KEY);
            key.SetValue(name, value, RegistryValueKind.String);
        }

        private static string? GetRegistryValue(string name)
        {
            try
            {
                using var key = Registry.CurrentUser.OpenSubKey(REGISTRY_KEY);
                return key?.GetValue(name) as string;
            }
            catch { return null; }
        }

        private static string? GetStoredInn()
        {
            return GetRegistryValue("INN");
        }

        public static string GetMacAddress()
        {
            try
            {
                var mac = NetworkInterface
                    .GetAllNetworkInterfaces()
                    .Where(nic => nic.OperationalStatus == OperationalStatus.Up
                               && nic.NetworkInterfaceType != NetworkInterfaceType.Loopback)
                    .OrderBy(nic => nic.Name)
                    .FirstOrDefault()
                    ?.GetPhysicalAddress()
                    .ToString();

                return mac ?? "00-00-00-00-00-00";
            }
            catch
            {
                return "00-00-00-00-00-00";
            }
        }

        private static string ComputeHmac(string key, string message)
        {
            using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(key));
            byte[] hash    = hmac.ComputeHash(Encoding.UTF8.GetBytes(message));
            return BitConverter.ToString(hash).Replace("-", "").ToLower();
        }

        private static ActivationResult Fail(string message) => new ActivationResult
        {
            Success = false,
            Status  = "error",
            Error   = message,
            Info    = message
        };
    }
}
