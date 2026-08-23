<?php

namespace App\Command;

use App\Entity\User;
use Doctrine\DBAL\Exception\UniqueConstraintViolationException;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputArgument;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;
use Symfony\Component\Console\Style\SymfonyStyle;

/**
 * Creates an account from the shell. The API has no registration endpoint, so
 * without this the only way to get a user is doctrine:fixtures:load, which
 * creates one fixed admin and wipes nothing else.
 *
 *   php bin/console app:user:create alice@example.com
 *   php bin/console app:user:create alice@example.com --password='...' --role=ROLE_ADMIN
 */
#[AsCommand(
    name: 'app:user:create',
    description: 'Create a user account from the command line',
)]
class CreateUserCommand extends AbstractUserCommand
{
    protected function configure(): void
    {
        $this
            ->addArgument('email', InputArgument::REQUIRED, 'Email address for the new account')
            ->addOption(
                'password',
                'p',
                InputOption::VALUE_REQUIRED,
                'Password. Omit to generate a random one and print it.'
            )
            ->addOption(
                'role',
                'r',
                InputOption::VALUE_REQUIRED | InputOption::VALUE_IS_ARRAY,
                'Grant a role (repeatable). ROLE_USER is always implied.'
            );
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $io = new SymfonyStyle($input, $output);

        $email = trim($input->getArgument('email'));

        if (!filter_var($email, FILTER_VALIDATE_EMAIL)) {
            $io->error(sprintf('"%s" is not a valid email address.', $email));

            return Command::FAILURE;
        }

        if (mb_strlen($email) > User::MAX_EMAIL_LENGTH) {
            $io->error(sprintf('Email must be at most %d characters.', User::MAX_EMAIL_LENGTH));

            return Command::FAILURE;
        }

        if ($this->entityManager->getRepository(User::class)->findOneBy(['email' => $email])) {
            $io->error(sprintf('An account already exists for "%s".', $email));

            return Command::FAILURE;
        }

        $roles = [];
        foreach ($input->getOption('role') as $role) {
            $role = strtoupper(trim($role));
            if (!str_starts_with($role, 'ROLE_')) {
                $io->error(sprintf('Invalid role "%s": roles must start with ROLE_.', $role));

                return Command::FAILURE;
            }
            $roles[] = $role;
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

        $user = (new User())->setEmail($email);
        $user->setPassword($this->passwordHasher->hashPassword($user, $password));
        // getRoles() always adds ROLE_USER, so an empty list is a normal account.
        $user->setRoles(array_values(array_unique($roles)));

        $this->entityManager->persist($user);

        try {
            $this->entityManager->flush();
        } catch (UniqueConstraintViolationException) {
            // The findOneBy above races with concurrent runs; the unique index on
            // email is what actually guarantees it.
            $io->error(sprintf('An account already exists for "%s".', $email));

            return Command::FAILURE;
        }

        $io->success(sprintf('Created %s.', $email));

        if ($generated) {
            // The only time this password is ever visible. It is not stored
            // anywhere in plain text.
            $io->writeln(sprintf('  Generated password: <info>%s</info>', $password));
        }

        $io->writeln(sprintf('  Roles: %s', implode(', ', $user->getRoles())));

        return Command::SUCCESS;
    }
}
