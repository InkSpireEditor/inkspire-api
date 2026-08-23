<?php

namespace App\Command;

use App\Entity\RefreshToken;
use App\Entity\User;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputArgument;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;
use Symfony\Component\Console\Style\SymfonyStyle;

/**
 * Administrative password reset, run from a shell with access to the database.
 *
 * There is deliberately no HTTP equivalent: the API exposes no way to change a
 * password, so this command is the only path. It requires no knowledge of the
 * current password, which is exactly why it must stay off the web.
 *
 *   php bin/console app:user:reset-password alice@example.com
 *   php bin/console app:user:reset-password alice@example.com --password='correct horse battery'
 */
#[AsCommand(
    name: 'app:user:reset-password',
    description: "Set a user's password from the command line and revoke their sessions",
)]
class ResetPasswordCommand extends AbstractUserCommand
{
    protected function configure(): void
    {
        $this
            ->addArgument('email', InputArgument::REQUIRED, 'Email address of the account to reset')
            ->addOption(
                'password',
                'p',
                InputOption::VALUE_REQUIRED,
                'New password. Omit to generate a random one and print it.'
            )
            ->addOption(
                'keep-sessions',
                null,
                InputOption::VALUE_NONE,
                'Leave existing refresh tokens valid instead of revoking them'
            );
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $io = new SymfonyStyle($input, $output);

        $email = $input->getArgument('email');
        $user = $this->entityManager->getRepository(User::class)->findOneBy(['email' => $email]);

        if (!$user) {
            $io->error(sprintf('No account found for "%s".', $email));

            return Command::FAILURE;
        }

        $password = $input->getOption('password');
        $generated = $password === null;

        if ($generated) {
            $password = $this->generatePassword();
        }

        if ($error = $this->validatePassword($password)) {
            $io->error($error);

            return Command::FAILURE;
        }

        $user->setPassword($this->passwordHasher->hashPassword($user, $password));

        // A password reset that leaves refresh tokens alive is not a reset: a
        // stolen token would keep minting JWTs for the full 7-day window. Revoke
        // unless the operator explicitly asks otherwise.
        $revoked = 0;
        if (!$input->getOption('keep-sessions')) {
            $tokens = $this->entityManager->getRepository(RefreshToken::class)->findBy(['user' => $user]);
            foreach ($tokens as $token) {
                $this->entityManager->remove($token);
            }
            $revoked = count($tokens);
        }

        $this->entityManager->flush();

        $io->success(sprintf('Password updated for %s.', $email));

        if ($generated) {
            // The only time this password is ever visible. It is not stored
            // anywhere in plain text.
            $io->writeln(sprintf('  Generated password: <info>%s</info>', $password));
            $io->newLine();
        }

        if ($input->getOption('keep-sessions')) {
            $io->warning('Existing sessions were left active (--keep-sessions).');
        } else {
            $io->writeln(sprintf('  Refresh tokens revoked: %d', $revoked));
        }

        return Command::SUCCESS;
    }
}
