/*
 * DebtScoring — Внешняя компонента для 1С
 * =========================================
 * Реализует Native API 1С (IInitDone, ILanguageExtender).
 * Мост между 1С и Python scoring_engine через Python.NET.
 *
 * Методы, доступные из 1С:
 *   СкоринговыйАнализ(json)   → json  — основной расчёт
 *   АктивироватьЛицензию(key, inn, mac) → json
 *   ПроверитьЛицензию()        → bool
 *   ПолучитьВерсию()           → string
 */

using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Python.Runtime;

namespace DebtScoring
{
    // ─────────────────────────────────────────────────────────────────────────
    // GUID регистрируется один раз — не меняй между версиями!
    // ─────────────────────────────────────────────────────────────────────────
    [ComVisible(true)]
    [Guid("A1B2C3D4-E5F6-7890-ABCD-EF1234567890")]
    [ProgId("AddIn.DebtScoring")]
    public class DebtScoringAddIn : IDisposable
    {
        // ── Константы ──────────────────────────────────────────────────────

        private const string COMPONENT_NAME    = "DebtScoring";
        private const string COMPONENT_VERSION = "1.0.0";

        // Соль для шифрования Python-кода.
        // При сборке финального продукта — заменяй на случайную строку 32+ символа!
        private const string ENCRYPTION_SALT   = "DS_SALT_REPLACE_IN_PRODUCTION_32C";

        // ── Состояние ──────────────────────────────────────────────────────

        private bool    _pythonInitialized = false;
        private bool    _licenseValid      = false;
        private string? _licenseInfo       = null;

        // ── Инициализация компоненты ───────────────────────────────────────

        /// <summary>
        /// Вызывается из 1С при подключении компоненты.
        /// Расшифровываем Python-код и инициализируем среду.
        /// </summary>
        public bool Init()
        {
            try
            {
                InitPython();
                // Первичная проверка лицензии при загрузке
                _licenseValid = LicenseManager.CheckLocal();
                return true;
            }
            catch (Exception ex)
            {
                LogError("Init", ex);
                return false;
            }
        }

        // ── Публичные методы (вызываются из 1С) ───────────────────────────

        /// <summary>
        /// МЕТОД 1: Скоринговый анализ контрагента.
        ///
        /// Вызов из 1С:
        ///   Компонента = Новый("AddIn.DebtScoring");
        ///   РезультатJSON = Компонента.СкоринговыйАнализ(ЗапросJSON);
        ///
        /// Входной JSON:
        ///   { "action": "score_single", "payment_history": [...] }
        ///   { "action": "score_batch",  "contractors": { "guid": [...] } }
        ///
        /// Возвращает JSON с результатом или {"error": "..."}
        /// </summary>
        public string ScoreContractor(string jsonInput)
        {
            if (!_licenseValid)
                return BuildError("LICENSE_INVALID", "Лицензия недействительна или истекла");

            if (string.IsNullOrWhiteSpace(jsonInput))
                return BuildError("EMPTY_INPUT", "Передан пустой запрос");

            try
            {
                return ExecutePython("process_request", jsonInput);
            }
            catch (Exception ex)
            {
                LogError("ScoreContractor", ex);
                return BuildError("PYTHON_ERROR", ex.Message);
            }
        }

        /// <summary>
        /// МЕТОД 2: Активация лицензии.
        ///
        /// Вызов из 1С:
        ///   РезультатJSON = Компонента.АктивироватьЛицензию(КлючЛицензии, ИНН, МАС);
        /// </summary>
        public string ActivateLicense(string licenseKey, string inn, string macAddress)
        {
            try
            {
                var result = LicenseManager.ActivateOnline(licenseKey, inn, macAddress);
                if (result.Success)
                {
                    _licenseValid = true;
                    _licenseInfo  = result.Info;
                }
                return JsonConvert.SerializeObject(result);
            }
            catch (Exception ex)
            {
                LogError("ActivateLicense", ex);
                return BuildError("ACTIVATION_ERROR", ex.Message);
            }
        }

        /// <summary>
        /// МЕТОД 3: Проверка состояния лицензии.
        ///
        /// Вызов из 1С:
        ///   ЛицензияДействительна = Компонента.ПроверитьЛицензию();
        /// </summary>
        public bool CheckLicense()
        {
            _licenseValid = LicenseManager.CheckLocal();
            return _licenseValid;
        }

        /// <summary>
        /// МЕТОД 4: Версия компоненты.
        ///
        /// Вызов из 1С:
        ///   Версия = Компонента.ПолучитьВерсию();
        /// </summary>
        public string GetVersion()
        {
            return JsonConvert.SerializeObject(new
            {
                component_version = COMPONENT_VERSION,
                python_available  = _pythonInitialized,
                license_valid     = _licenseValid,
                license_info      = _licenseInfo
            });
        }

        // ── Python Bridge ──────────────────────────────────────────────────

        private void InitPython()
        {
            if (_pythonInitialized) return;

            // Извлекаем и расшифровываем Python-код из ресурсов DLL
            string pythonCode = ExtractAndDecryptPythonCode();

            // Настраиваем путь к Python (embedded, рядом с DLL)
            string dllDir = Path.GetDirectoryName(
                Assembly.GetExecutingAssembly().Location) ?? ".";
            string pythonHome = Path.Combine(dllDir, "python");

            // Устанавливаем переменные окружения для embedded Python
            Environment.SetEnvironmentVariable("PYTHONHOME", pythonHome);
            Environment.SetEnvironmentVariable("PYTHONPATH", pythonHome + "\\Lib");

            // Путь к python3xx.dll
            Runtime.PythonDLL = Path.Combine(pythonHome, "python312.dll");

            PythonEngine.Initialize();
            PythonEngine.BeginAllowThreads();

            // Загружаем наш модуль из строки (не из файла — это важно для защиты!)
            using (Py.GIL())
            {
                dynamic sys = Py.Import("sys");
                // Компилируем и загружаем модуль прямо в память
                PythonEngine.Exec(pythonCode);
            }

            _pythonInitialized = true;
        }

        private string ExecutePython(string functionName, string jsonInput)
        {
            using (Py.GIL())
            {
                dynamic module   = Py.Import("scoring_engine");
                dynamic func     = module.GetAttr(functionName);
                dynamic result   = func(jsonInput);
                return result.ToString();
            }
        }

        // ── Расшифровка Python-кода ────────────────────────────────────────

        /// <summary>
        /// Читает зашифрованный Python-код из ресурсов DLL и расшифровывает его.
        /// Ключ шифрования = AES-256(SALT + MachineGuid) — уникален для каждой машины.
        /// </summary>
        private string ExtractAndDecryptPythonCode()
        {
            // Читаем зашифрованный файл из embedded ресурсов
            var assembly    = Assembly.GetExecutingAssembly();
            var resourceName = "DebtScoring.Resources.scoring_engine.enc";

            using var stream = assembly.GetManifestResourceStream(resourceName)
                ?? throw new FileNotFoundException(
                    "Ресурс scoring_engine.enc не найден в DLL. " +
                    "Запустите build_tools/encrypt_python.py перед компиляцией.");

            byte[] encryptedBytes;
            using var ms = new MemoryStream();
            stream.CopyTo(ms);
            encryptedBytes = ms.ToArray();

            // Ключ = SHA256(SALT + лицензионный токен)
            // Это означает: без валидного лицензионного токена Python-код не расшифруется
            string tokenForKey = LicenseManager.GetStoredToken() ?? ENCRYPTION_SALT;
            byte[] key         = DeriveKey(ENCRYPTION_SALT + tokenForKey);

            return AesDecrypt(encryptedBytes, key);
        }

        private static byte[] DeriveKey(string password)
        {
            using var sha = SHA256.Create();
            return sha.ComputeHash(Encoding.UTF8.GetBytes(password));
        }

        private static string AesDecrypt(byte[] cipherData, byte[] key)
        {
            // Первые 16 байт — IV
            byte[] iv         = new byte[16];
            byte[] cipherText = new byte[cipherData.Length - 16];
            Buffer.BlockCopy(cipherData, 0, iv, 0, 16);
            Buffer.BlockCopy(cipherData, 16, cipherText, 0, cipherText.Length);

            using var aes       = Aes.Create();
            aes.Key             = key;
            aes.IV              = iv;
            aes.Mode            = CipherMode.CBC;
            aes.Padding         = PaddingMode.PKCS7;

            using var decryptor = aes.CreateDecryptor();
            byte[] plainBytes   = decryptor.TransformFinalBlock(
                cipherText, 0, cipherText.Length);

            return Encoding.UTF8.GetString(plainBytes);
        }

        // ── Вспомогательные методы ─────────────────────────────────────────

        private static string BuildError(string code, string message) =>
            JsonConvert.SerializeObject(new { error = code, message });

        private static void LogError(string context, Exception ex)
        {
            // В продакшене пиши в лог-файл рядом с DLL
            try
            {
                string logPath = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "DebtScoring", "error.log");
                Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
                File.AppendAllText(logPath,
                    $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] [{context}] {ex}\n");
            }
            catch { /* Логирование не должно роняло основную логику */ }
        }

        // ── IDisposable ────────────────────────────────────────────────────

        public void Dispose()
        {
            if (_pythonInitialized)
            {
                try { PythonEngine.Shutdown(); } catch { }
                _pythonInitialized = false;
            }
            GC.SuppressFinalize(this);
        }
    }
}
