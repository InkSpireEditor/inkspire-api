<?php

namespace App\Command;

use App\Form\RegistrationFormType;
use Doctrine\ORM\EntityManagerInterface;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\PasswordHasher\Hasher\UserPasswordHasherInterface;

/**
 * Shared plumbing for the administrative user commands.
 *
 * These commands are shell-only by design: neither has an HTTP equivalent, and
 * both act without knowing the account's current password.
 */
abstract class AbstractUserCommand extends Command
{
    /**
     * Bytes of randomness behind a generated password. Hex-encoded, so the
     * printed string is twice this long.
     */
    protected const GENERATED_PASSWORD_BYTES = 12;

    public function __construct(
        protected readonly EntityManagerInterface $entityManager,
        protected readonly UserPasswordHasherInterface $passwordHasher,
    ) {
        parent::__construct();
    }

    protected function generatePassword(): string
    {
        return bin2hex(random_bytes(self::GENERATED_PASSWORD_BYTES));
    }

    /**
     * Checks a password against the same policy the registration form enforces,
     * so a password set from the CLI cannot be one the application would have
     * rejected.
     *
     * @return string|null An error message, or null when the password is acceptable.
     */
    protected function validatePassword(string $password): ?string
    {
        if (mb_strlen($password) < RegistrationFormType::MIN_PASSWORD_LENGTH) {
            return sprintf(
                'Password must be at least %d characters.',
                RegistrationFormType::MIN_PASSWORD_LENGTH
            );
        }

        if (mb_strlen($password) > RegistrationFormType::MAX_PASSWORD_LENGTH) {
            return sprintf(
                'Password must be at most %d characters.',
                RegistrationFormType::MAX_PASSWORD_LENGTH
            );
        }

        return null;
    }
}
